"""Buffer-local external detectors. They receive audio + rate + params only."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from referee.types import BufferScope

from adapter.external_baselines import (
    ExternalBaselineSpec,
    FFMPEG_BINARY,
    openvpi_slicer2_path,
    require_openvpi_vendor,
)
from adapter.external_ffmpeg import run_silencedetect_on_wav

WALL_EPS_SEC = 0.001


@dataclass(frozen=True)
class ExternalAudioRequest:
    """Annotation-free input to an external detector family."""

    recording_id: str
    sample_rate_hz: int
    buffers: tuple[BufferScope, ...]
    audio: Any
    spec: ExternalBaselineSpec

    def assert_firewall(self) -> None:
        names = {item.name for item in fields(self)}
        if names != {"recording_id", "sample_rate_hz", "buffers", "audio", "spec"}:
            raise RuntimeError(f"external request leaked fields: {sorted(names)}")
        for forbidden in (
            "phones",
            "words",
            "uncertainty_intervals",
            "reference",
            "annotations",
        ):
            if hasattr(self, forbidden):
                raise RuntimeError(f"external request carries annotation field {forbidden!r}")


@dataclass(frozen=True)
class BufferDetection:
    buffer_id: str
    recording_id: str
    buffer_start_sec: float
    buffer_end_sec: float
    native_clips: tuple[tuple[float, float], ...]
    candidate_times: tuple[float, ...]
    detector_runtime_sec: float


@dataclass(frozen=True)
class LocalDetection:
    native_clips_local: tuple[tuple[float, float], ...]
    candidate_times_local: tuple[float, ...]


def map_local_to_recording(local_sec: float, buffer_start_sec: float) -> float:
    return float(buffer_start_sec) + float(local_sec)


def clamp_recording_time(time_sec: float, buffer: BufferScope) -> float:
    return min(max(float(time_sec), float(buffer.start_sec)), float(buffer.end_sec))


def extract_buffer_audio(audio: Any, sample_rate_hz: int, buffer: BufferScope) -> Any:
    start = int(round(float(buffer.start_sec) * sample_rate_hz))
    end = int(round(float(buffer.end_sec) * sample_rate_hz))
    n = int(audio.shape[0])
    start = max(0, min(start, n))
    end = max(start, min(end, n))
    return audio[start:end]


def _local_duration_sec(samples: Any, sample_rate_hz: int) -> float:
    return float(samples.shape[0]) / float(sample_rate_hz) if sample_rate_hz else 0.0


def _clip_local_interval(
    start: float,
    end: float,
    duration_sec: float,
) -> tuple[float, float] | None:
    lo = max(0.0, float(start))
    hi = min(float(duration_sec), float(end))
    if hi <= lo:
        return None
    return (lo, hi)


def _interior_times(
    times: list[float],
    *,
    duration_sec: float,
) -> tuple[float, ...]:
    unique: list[float] = []
    seen: set[float] = set()
    for raw in times:
        value = float(raw)
        if not (WALL_EPS_SEC < value < duration_sec - WALL_EPS_SEC):
            continue
        key = round(value, 9)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    unique.sort()
    return tuple(unique)


def _nonsilent_from_silence(
    silences: list[tuple[float, float]],
    duration_sec: float,
) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(silences):
        lo = max(0.0, float(start))
        hi = min(float(duration_sec), float(end))
        if hi <= lo:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_sec:
        regions.append((cursor, duration_sec))
    return regions


def _load_openvpi_module() -> Any:
    require_openvpi_vendor()
    path = openvpi_slicer2_path()
    spec = importlib.util.spec_from_file_location("openvpi_audio_slicer_slicer2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load OpenVPI slicer from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _openvpi_chunk_spans(slicer: Any, waveform: Any) -> tuple[list[Any], list[tuple[int, int]]]:
    """Recover hop spans without editing vendored slicer2.py."""
    spans: list[tuple[int, int]] = []
    original = slicer._apply_slice

    def tracked(samples: Any, begin: int, end: int) -> Any:
        spans.append((int(begin), int(end)))
        return original(samples, begin, end)

    slicer._apply_slice = tracked
    try:
        chunks = slicer.slice(waveform)
    finally:
        slicer._apply_slice = original
    return list(chunks), spans


def detect_openvpi_local(
    samples: Any,
    sample_rate_hz: int,
    params: dict[str, Any],
) -> LocalDetection:
    module = _load_openvpi_module()
    slicer = module.Slicer(
        sr=int(sample_rate_hz),
        threshold=float(params["threshold_db"]),
        min_length=int(params["min_length_ms"]),
        min_interval=int(params["min_interval_ms"]),
        hop_size=int(params["hop_size_ms"]),
        max_sil_kept=int(params["max_sil_kept_ms"]),
    )
    duration_sec = _local_duration_sec(samples, sample_rate_hz)
    _chunks, spans = _openvpi_chunk_spans(slicer, samples)
    hop = int(slicer.hop_size)
    n = int(samples.shape[0])
    clips: list[tuple[float, float]] = []
    candidates: list[float] = []
    if not spans:
        interval = _clip_local_interval(0.0, duration_sec, duration_sec)
        if interval is not None:
            clips.append(interval)
        return LocalDetection(
            native_clips_local=tuple(clips),
            candidate_times_local=_interior_times(candidates, duration_sec=duration_sec),
        )
    for begin, end in spans:
        start_sec = (begin * hop) / float(sample_rate_hz)
        end_sample = min(n, end * hop)
        end_sec = end_sample / float(sample_rate_hz)
        interval = _clip_local_interval(start_sec, end_sec, duration_sec)
        if interval is not None:
            clips.append(interval)
            candidates.append(interval[0])
            candidates.append(interval[1])
    return LocalDetection(
        native_clips_local=tuple(clips),
        candidate_times_local=_interior_times(candidates, duration_sec=duration_sec),
    )


def detect_librosa_local(
    samples: Any,
    sample_rate_hz: int,
    params: dict[str, Any],
) -> LocalDetection:
    import librosa
    import numpy as np

    duration_sec = _local_duration_sec(samples, sample_rate_hz)
    intervals = librosa.effects.split(
        np.asarray(samples),
        top_db=float(params["top_db"]),
        frame_length=int(params["frame_length"]),
        hop_length=int(params["hop_length"]),
    )
    clips: list[tuple[float, float]] = []
    candidates: list[float] = []
    for start_sample, end_sample in intervals:
        start_sec = float(start_sample) / float(sample_rate_hz)
        end_sec = float(end_sample) / float(sample_rate_hz)
        interval = _clip_local_interval(start_sec, end_sec, duration_sec)
        if interval is None:
            continue
        clips.append(interval)
        candidates.append(interval[0])
        candidates.append(interval[1])
    return LocalDetection(
        native_clips_local=tuple(clips),
        candidate_times_local=_interior_times(candidates, duration_sec=duration_sec),
    )


def detect_pydub_local(
    samples: Any,
    sample_rate_hz: int,
    params: dict[str, Any],
) -> LocalDetection:
    import numpy as np
    from pydub import AudioSegment
    from pydub.silence import detect_nonsilent, detect_silence

    duration_sec = _local_duration_sec(samples, sample_rate_hz)
    pcm = np.clip(np.round(np.asarray(samples) * 32767.0), -32768, 32767).astype(np.int16)
    segment = AudioSegment(
        data=pcm.tobytes(),
        sample_width=2,
        frame_rate=int(sample_rate_hz),
        channels=1,
    )
    min_silence_len = int(params["min_silence_len_ms"])
    silence_thresh = float(params["silence_thresh_dbfs"])
    seek_step = int(params["seek_step_ms"])
    silent = detect_silence(
        segment,
        min_silence_len=min_silence_len,
        silence_thresh=silence_thresh,
        seek_step=seek_step,
    )
    spoken = detect_nonsilent(
        segment,
        min_silence_len=min_silence_len,
        silence_thresh=silence_thresh,
        seek_step=seek_step,
    )
    clips: list[tuple[float, float]] = []
    for start_ms, end_ms in spoken:
        interval = _clip_local_interval(start_ms / 1000.0, end_ms / 1000.0, duration_sec)
        if interval is not None:
            clips.append(interval)
    candidates: list[float] = []
    for start_ms, end_ms in silent:
        start_sec = start_ms / 1000.0
        end_sec = end_ms / 1000.0
        if end_sec <= start_sec:
            continue
        candidates.append(0.5 * (start_sec + end_sec))
    return LocalDetection(
        native_clips_local=tuple(clips),
        candidate_times_local=_interior_times(candidates, duration_sec=duration_sec),
    )


def detect_ffmpeg_local(
    samples: Any,
    sample_rate_hz: int,
    params: dict[str, Any],
) -> LocalDetection:
    import soundfile as sf

    duration_sec = _local_duration_sec(samples, sample_rate_hz)
    binary = str(params.get("binary") or FFMPEG_BINARY)
    filter_arg = str(params.get("filter") or "silencedetect=noise=-60dB:duration=2")
    with tempfile.TemporaryDirectory(prefix="buckeye_ffmpeg_buf_") as tmp:
        wav_path = Path(tmp) / "buffer.wav"
        sf.write(str(wav_path), samples, int(sample_rate_hz), subtype="PCM_16")
        silences, _stderr = run_silencedetect_on_wav(
            wav_path,
            duration_sec=duration_sec,
            binary=binary,
            filter_arg=filter_arg,
        )
    clips = [
        interval
        for start, end in _nonsilent_from_silence(silences, duration_sec)
        if (interval := _clip_local_interval(start, end, duration_sec)) is not None
    ]
    candidates = [0.5 * (start + end) for start, end in silences if end > start]
    return LocalDetection(
        native_clips_local=tuple(clips),
        candidate_times_local=_interior_times(candidates, duration_sec=duration_sec),
    )


_DETECTORS = {
    "openvpi": detect_openvpi_local,
    "librosa": detect_librosa_local,
    "pydub": detect_pydub_local,
    "ffmpeg": detect_ffmpeg_local,
}


def detect_buffer(
    request: ExternalAudioRequest,
    buffer: BufferScope,
) -> BufferDetection:
    request.assert_firewall()
    if buffer.buffer_id not in {item.buffer_id for item in request.buffers}:
        raise RuntimeError(f"buffer {buffer.buffer_id!r} is not in the allowed set")
    samples = extract_buffer_audio(request.audio, request.sample_rate_hz, buffer)
    detector = _DETECTORS.get(request.spec.family)
    if detector is None:
        raise RuntimeError(f"unknown external family {request.spec.family!r}")
    started = time.perf_counter()
    local = detector(samples, request.sample_rate_hz, request.spec.params)
    runtime = time.perf_counter() - started
    native: list[tuple[float, float]] = []
    for start, end in local.native_clips_local:
        rec_start = clamp_recording_time(
            map_local_to_recording(start, buffer.start_sec), buffer
        )
        rec_end = clamp_recording_time(
            map_local_to_recording(end, buffer.start_sec), buffer
        )
        if rec_end <= rec_start:
            continue
        native.append((rec_start, rec_end))
    candidates: list[float] = []
    for local_t in local.candidate_times_local:
        rec_t = clamp_recording_time(
            map_local_to_recording(local_t, buffer.start_sec), buffer
        )
        if not (buffer.start_sec + WALL_EPS_SEC < rec_t < buffer.end_sec - WALL_EPS_SEC):
            continue
        candidates.append(rec_t)
    return BufferDetection(
        buffer_id=buffer.buffer_id,
        recording_id=request.recording_id,
        buffer_start_sec=float(buffer.start_sec),
        buffer_end_sec=float(buffer.end_sec),
        native_clips=tuple(native),
        candidate_times=tuple(candidates),
        detector_runtime_sec=float(runtime),
    )


def detect_recording(request: ExternalAudioRequest) -> tuple[BufferDetection, ...]:
    request.assert_firewall()
    return tuple(detect_buffer(request, buffer) for buffer in request.buffers)
