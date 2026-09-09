"""Frozen VR slicer: Silero ONNX frames + percentile RMS + duration-only packer.

Authoritative source (do not import at runtime):

- buckeye-slicer-lab ``O0_4`` / ``handoff/VR_SLICER.md``
- ``speaker_ts_eval.slicer_daddy_test.VadPercentileRmsDetector``
- ``speaker_ts_eval.repaired_buckeye_benchmark.generate_legal_candidate_clips_in_buffers``
- ``speaker_ts_eval.buckeye_safecut_benchmark._compute_recording_vad_frames``

Production fingerprint hashes every ``VrGeometry`` field that affects VAD,
RMS cutpoints, or packing. The lab's older window/hop/offset hash is not
this value.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence


EPSILON_SEC = 1e-9
BOUNDARY_EPSILON_SEC = 0.001
TRUSTED_GEOMETRY_FINGERPRINT = (
    "0ecfe5c7b67aeec535d6cc7435e8e43f3269487d88bb0cefc69891bb86e186d7"
)
DETECTOR_NAME = "vad_percentile_rms"
PACKER_NAME = "optimal_weighted_interval"


@dataclass(frozen=True)
class VrGeometry:
    """Locked O0_4 knobs. Do not retune."""

    name: str = "O0_4"
    window_samples: int = 512
    hop_samples: int = 512
    offsets: tuple[int, ...] = (0, 64, 128, 192, 256, 320, 384, 448)
    sample_rate_hz: int = 16000
    vad_backend: str = "silero_official_onnx"
    vad_threshold: float = 0.2
    vad_min_run_ms: float = 40.0
    percentile_rms_percentile: float = 10.0
    percentile_rms_margin_db: float = 3.0
    min_clip_sec: float = 3.0
    preferred_min_sec: float = 6.0
    preferred_max_sec: float = 8.0
    target_clip_sec: float = 8.0
    max_clip_sec: float = 15.0


VR_O0_4 = VrGeometry()


@dataclass(frozen=True)
class BufferScope:
    buffer_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class VrCutpoint:
    detector_name: str
    source_id: str
    recording_id: str
    buffer_id: str
    cutpoint_id: str
    time_sec: float
    interval_start_sec: float
    interval_end_sec: float
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VrPackedClip:
    detector_name: str
    packer_name: str
    source_id: str
    recording_id: str
    buffer_id: str
    clip_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    start_cutpoint_id: str
    end_cutpoint_id: str
    selected_weight: float


@dataclass(frozen=True)
class VrSlicerResult:
    geometry_fingerprint: str
    cutpoints: tuple[VrCutpoint, ...]
    clips: tuple[VrPackedClip, ...]
    selected_cutpoints: tuple[VrCutpoint, ...]


def _canonicalize_geometry_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_canonicalize_geometry_value(item) for item in value]
    if isinstance(value, list):
        return [_canonicalize_geometry_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonicalize_geometry_value(item) for key, item in value.items()}
    return value


def geometry_canonical_payload(geometry: VrGeometry) -> dict[str, Any]:
    return _canonicalize_geometry_value(asdict(geometry))


def geometry_fingerprint(geometry: VrGeometry = VR_O0_4) -> str:
    payload = json.dumps(
        geometry_canonical_payload(geometry),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_locked_geometry(geometry: VrGeometry = VR_O0_4) -> str:
    fingerprint = geometry_fingerprint(geometry)
    if fingerprint != TRUSTED_GEOMETRY_FINGERPRINT:
        raise RuntimeError(
            f"VR slicer geometry drifted: {fingerprint} != {TRUSTED_GEOMETRY_FINGERPRINT}"
        )
    return fingerprint


def overlap_duration(
    left_start: float,
    left_end: float,
    right_start: float,
    right_end: float,
) -> float:
    if not (left_start < left_end and right_start < right_end):
        raise ValueError("interval start must be < end")
    return max(0.0, min(left_end, right_end) - max(left_start, right_start))


def merge_intervals(
    intervals: list[tuple[float, float]],
    *,
    epsilon_sec: float = 0.0,
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    ordered = sorted((float(start), float(end)) for start, end in intervals)
    for start_sec, end_sec in ordered:
        if start_sec >= end_sec:
            raise ValueError(f"interval start must be < end: {start_sec}, {end_sec}")
        if not merged or start_sec > (merged[-1][1] + epsilon_sec):
            merged.append([start_sec, end_sec])
        else:
            merged[-1][1] = max(merged[-1][1], end_sec)
    return [(start_sec, end_sec) for start_sec, end_sec in merged]


def _percentile_value(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot compute percentile of an empty list")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _candidate_score(*, speech_prob: float, rms_dbfs: float) -> float:
    return (1.0 - speech_prob) + max(0.0, min(1.0, (-rms_dbfs) / 100.0))


def _stats_from_frames(frames: list[dict[str, Any]]) -> dict[str, Any]:
    if not frames:
        return {
            "vad_min": None,
            "vad_mean": None,
            "rms_min_dbfs": None,
            "rms_mean_dbfs": None,
            "voicing_min": None,
            "voicing_mean": None,
        }
    vad_probs = [float(row["speech_prob"]) for row in frames]
    rms_values = [float(row["rms_dbfs"]) for row in frames]
    voicing_values = [float(row["voicing_score"]) for row in frames]
    return {
        "vad_min": min(vad_probs),
        "vad_mean": statistics.fmean(vad_probs),
        "rms_min_dbfs": min(rms_values),
        "rms_mean_dbfs": statistics.fmean(rms_values),
        "voicing_min": min(voicing_values),
        "voicing_mean": statistics.fmean(voicing_values),
    }


def compute_vad_frames(
    samples: Any,
    sample_rate: int,
    geometry: VrGeometry = VR_O0_4,
) -> list[dict[str, Any]]:
    import numpy as np
    import torch
    from silero_vad import load_silero_vad

    if int(sample_rate) != int(geometry.sample_rate_hz):
        raise ValueError(
            f"audio sample rate {sample_rate} != VR geometry {geometry.sample_rate_hz}"
        )
    if geometry.vad_backend != "silero_official_onnx":
        raise ValueError(f"unsupported VR VAD backend: {geometry.vad_backend}")
    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        audio = audio[:, 0]
    model = load_silero_vad(onnx=True)
    rows: list[dict[str, Any]] = []
    try:
        for offset in geometry.offsets:
            model.reset_states()
            for start in range(offset, len(audio), geometry.hop_samples):
                chunk = audio[start : start + geometry.window_samples]
                if len(chunk) < geometry.window_samples:
                    chunk = np.pad(chunk, (0, geometry.window_samples - len(chunk)))
                prob = float(model(torch.from_numpy(chunk), sample_rate).item())
                center = start + (geometry.window_samples // 2)
                rows.append(
                    {
                        "window_start_sec": round(start / sample_rate, 6),
                        "window_end_sec": round((start + geometry.window_samples) / sample_rate, 6),
                        "center_sec": round(center / sample_rate, 6),
                        "offset_samples": int(offset),
                        "speech_prob": prob,
                    }
                )
    finally:
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    rows.sort(key=lambda row: (row["center_sec"], row["offset_samples"]))
    return rows


def attach_rms_voicing(
    frames: list[dict[str, Any]],
    samples: Any,
    sample_rate: int,
) -> list[dict[str, Any]]:
    import numpy as np

    audio = np.asarray(samples, dtype=np.float32)
    if audio.ndim != 1:
        audio = audio[:, 0]
    enriched: list[dict[str, Any]] = []
    for row in frames:
        start = max(0, int(round(float(row["window_start_sec"]) * sample_rate)))
        end = min(len(audio), int(round(float(row["window_end_sec"]) * sample_rate)))
        chunk = audio[start:end]
        if len(chunk) == 0:
            rms = 0.0
            zcr = 0.0
        else:
            rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64)))
            signs = np.signbit(chunk)
            zcr = float(np.mean(signs[1:] != signs[:-1])) if len(chunk) > 1 else 0.0
        rms_dbfs = -120.0 if rms <= 1e-8 else float(20.0 * np.log10(rms))
        voicing_score = max(0.0, min(1.0, 1.0 - min(zcr / 0.5, 1.0)))
        enriched.append(
            {
                "window_start_sec": row["window_start_sec"],
                "window_end_sec": row["window_end_sec"],
                "center_sec": row["center_sec"],
                "speech_prob": row["speech_prob"],
                "rms_dbfs": round(rms_dbfs, 6),
                "voicing_score": round(voicing_score, 6),
                "offset_samples": row["offset_samples"],
            }
        )
    return enriched


def _cut_placement_key(row: dict[str, Any], *, run_start: float, run_end: float) -> tuple[float, ...]:
    run_center = (run_start + run_end) / 2.0
    return (
        float(row["speech_prob"]),
        float(row["rms_dbfs"]),
        abs(float(row["center_sec"]) - run_center),
    )


def _build_cutpoint(
    *,
    source_id: str,
    recording_id: str,
    buffer_id: str,
    region_index: int,
    run_index: int,
    run_frames: list[dict[str, Any]],
    run_start: float,
    run_end: float,
) -> VrCutpoint:
    valley = min(run_frames, key=lambda row: _cut_placement_key(row, run_start=run_start, run_end=run_end))
    stats = _stats_from_frames(run_frames)
    score = _candidate_score(
        speech_prob=float(valley["speech_prob"]),
        rms_dbfs=float(valley["rms_dbfs"]),
    )
    center = float(valley["center_sec"])
    run_ms = (run_end - run_start) * 1000.0
    cut_id = f"{buffer_id}-{DETECTOR_NAME}-{region_index:04d}-{run_index:04d}-{int(round(center * 1000.0))}"
    return VrCutpoint(
        detector_name=DETECTOR_NAME,
        source_id=source_id,
        recording_id=recording_id,
        buffer_id=buffer_id,
        cutpoint_id=cut_id,
        time_sec=round(center, 6),
        interval_start_sec=round(run_start, 6),
        interval_end_sec=round(run_end, 6),
        score=round(score, 6),
        metadata={
            "run_duration_ms": round(run_ms, 3),
            "selected_center_sec": round(center, 6),
            "vad_min": stats["vad_min"],
            "rms_min_dbfs": stats["rms_min_dbfs"],
        },
    )


def dedupe_cutpoints(cuts: Sequence[VrCutpoint]) -> list[VrCutpoint]:
    grouped: dict[tuple[str, int], list[VrCutpoint]] = defaultdict(list)
    for cut in cuts:
        grouped[(cut.recording_id, int(round(cut.time_sec * 1000.0)))].append(cut)
    deduped: list[VrCutpoint] = []
    for _, rows in grouped.items():
        best = max(
            rows,
            key=lambda cut: (
                cut.score,
                -len(cut.buffer_id),
                cut.buffer_id,
                cut.cutpoint_id,
            ),
        )
        scores = [float(row.score) for row in rows]
        metadata = dict(best.metadata)
        metadata.update(
            {
                "dedupe_representative_cutpoint_id": best.cutpoint_id,
                "contributing_buffer_ids": sorted({row.buffer_id for row in rows}),
                "contributing_cutpoint_ids": sorted(row.cutpoint_id for row in rows),
                "support_count": len(rows),
                "score_max": round(max(scores), 6),
                "score_mean": round(statistics.fmean(scores), 6),
                "score_min": round(min(scores), 6),
            }
        )
        deduped.append(replace(best, metadata=metadata))
    return sorted(deduped, key=lambda row: (row.recording_id, row.time_sec, row.cutpoint_id))


def detect_cutpoints(
    *,
    frames: list[dict[str, Any]],
    buffers: Sequence[BufferScope],
    source_id: str,
    recording_id: str,
    geometry: VrGeometry = VR_O0_4,
) -> list[VrCutpoint]:
    import numpy as np

    if not frames:
        return []
    centers = np.asarray([float(row["center_sec"]) for row in frames], dtype=np.float64)
    starts = np.asarray([float(row["window_start_sec"]) for row in frames], dtype=np.float64)
    ends = np.asarray([float(row["window_end_sec"]) for row in frames], dtype=np.float64)
    probs = np.asarray([float(row["speech_prob"]) for row in frames], dtype=np.float64)
    rms_dbfs = np.asarray([float(row["rms_dbfs"]) for row in frames], dtype=np.float64)
    cuts: list[VrCutpoint] = []
    for buffer in sorted(buffers, key=lambda row: row.buffer_id):
        trusted_start = float(buffer.start_sec)
        trusted_end = float(buffer.end_sec)
        if trusted_end - trusted_start < float(geometry.vad_min_run_ms) / 1000.0:
            continue
        left = int(np.searchsorted(centers, trusted_start + EPSILON_SEC, side="right"))
        right = int(np.searchsorted(centers, trusted_end - EPSILON_SEC, side="left"))
        region_indices = np.arange(left, right, dtype=np.int64)
        if len(region_indices) == 0:
            continue
        vad_mask = probs[region_indices] < float(geometry.vad_threshold)
        if not bool(vad_mask.any()):
            continue
        rms_threshold = _percentile_value(
            [float(value) for value in rms_dbfs[region_indices]],
            float(geometry.percentile_rms_percentile),
        ) + float(geometry.percentile_rms_margin_db)
        selected_positions = np.flatnonzero(vad_mask & (rms_dbfs[region_indices] <= rms_threshold))
        selected_indices = region_indices[selected_positions]
        if len(selected_indices) == 0:
            continue
        runs: list[tuple[float, float, int, int]] = []
        run_start = float(starts[selected_indices[0]])
        run_end = float(ends[selected_indices[0]])
        first_pos = 0
        last_pos = 0
        for pos, idx in enumerate(selected_indices[1:], start=1):
            frame_start = float(starts[idx])
            frame_end = float(ends[idx])
            if frame_start <= run_end + EPSILON_SEC:
                run_end = max(run_end, frame_end)
                last_pos = pos
            else:
                runs.append((run_start, run_end, first_pos, last_pos))
                run_start = frame_start
                run_end = frame_end
                first_pos = pos
                last_pos = pos
        runs.append((run_start, run_end, first_pos, last_pos))
        region_index = 0
        for run_index, (run_start, run_end, first_pos, last_pos) in enumerate(runs):
            run_ms = (run_end - run_start) * 1000.0
            if run_ms < geometry.vad_min_run_ms:
                continue
            run_frames = [frames[int(idx)] for idx in selected_indices[first_pos : last_pos + 1]]
            if not run_frames:
                continue
            cuts.append(
                _build_cutpoint(
                    source_id=source_id,
                    recording_id=recording_id,
                    buffer_id=buffer.buffer_id,
                    region_index=region_index,
                    run_index=run_index,
                    run_frames=run_frames,
                    run_start=run_start,
                    run_end=run_end,
                )
            )
    return dedupe_cutpoints(cuts)


def candidate_clip_weight(*, duration_sec: float, geometry: VrGeometry) -> float:
    if geometry.preferred_min_sec <= duration_sec <= geometry.preferred_max_sec:
        return 10.0 - ((geometry.target_clip_sec - duration_sec) * 1.5)
    if duration_sec > geometry.preferred_max_sec:
        return 6.5 - max(0.0, duration_sec - 9.0)
    return duration_sec / max(geometry.min_clip_sec, EPSILON_SEC)


@dataclass(frozen=True)
class _LegalCandidate:
    recording_id: str
    buffer_id: str
    start_sec: float
    end_sec: float
    duration_sec: float
    start_cutpoint_id: str
    end_cutpoint_id: str
    weight: float
    source_id: str


def generate_legal_candidates(
    cutpoints: Sequence[VrCutpoint],
    geometry: VrGeometry = VR_O0_4,
) -> list[_LegalCandidate]:
    by_buffer: dict[tuple[str, str], list[VrCutpoint]] = defaultdict(list)
    for cut in cutpoints:
        by_buffer[(cut.recording_id, cut.buffer_id)].append(cut)
    candidates: list[_LegalCandidate] = []
    for (recording_id, buffer_id), rows in sorted(by_buffer.items()):
        ordered = sorted(rows, key=lambda row: (row.time_sec, row.cutpoint_id))
        for start_index, start in enumerate(ordered[:-1]):
            for end in ordered[start_index + 1 :]:
                duration_sec = end.time_sec - start.time_sec
                if duration_sec < geometry.min_clip_sec - BOUNDARY_EPSILON_SEC:
                    continue
                if duration_sec > geometry.max_clip_sec + BOUNDARY_EPSILON_SEC:
                    break
                candidates.append(
                    _LegalCandidate(
                        recording_id=recording_id,
                        buffer_id=buffer_id,
                        start_sec=round(start.time_sec, 6),
                        end_sec=round(end.time_sec, 6),
                        duration_sec=round(duration_sec, 6),
                        start_cutpoint_id=start.cutpoint_id,
                        end_cutpoint_id=end.cutpoint_id,
                        weight=round(
                            candidate_clip_weight(duration_sec=duration_sec, geometry=geometry),
                            6,
                        ),
                        source_id=start.source_id,
                    )
                )
    return candidates


def _assert_schedule(rows: list[dict[str, Any]], *, max_overlap_sec: float) -> None:
    for left, right in zip(rows, rows[1:]):
        overlap = overlap_duration(
            float(left["start_sec"]),
            float(left["end_sec"]),
            float(right["start_sec"]),
            float(right["end_sec"]),
        )
        if overlap > max_overlap_sec + EPSILON_SEC:
            raise ValueError("selected clips exceed configured overlap")


def weighted_interval_schedule(
    candidates: Sequence[_LegalCandidate],
    *,
    max_overlap_sec: float = 0.0,
) -> list[_LegalCandidate]:
    ordered = sorted(candidates, key=lambda row: (float(row.end_sec), float(row.start_sec)))
    if not ordered:
        return []
    payload = [
        {
            "candidate": candidate,
            "start_sec": candidate.start_sec,
            "end_sec": candidate.end_sec,
            "weight": candidate.weight,
        }
        for candidate in ordered
    ]
    prev_compatible: list[int] = []
    for row in payload:
        compatible_index = -1
        for index in range(len(payload) - 1, -1, -1):
            if float(payload[index]["end_sec"]) <= float(row["start_sec"]) + max_overlap_sec + EPSILON_SEC:
                compatible_index = index
                break
        prev_compatible.append(compatible_index)
    best = [0.0] * (len(payload) + 1)
    for i, row in enumerate(payload, start=1):
        include = float(row["weight"]) + best[prev_compatible[i - 1] + 1]
        exclude = best[i - 1]
        best[i] = max(include, exclude)
    selected: list[dict[str, Any]] = []
    i = len(payload)
    while i > 0:
        row = payload[i - 1]
        include = float(row["weight"]) + best[prev_compatible[i - 1] + 1]
        if include >= best[i - 1] - 1e-12:
            selected.append(row)
            i = prev_compatible[i - 1] + 1
        else:
            i -= 1
    selected.reverse()
    _assert_schedule(selected, max_overlap_sec=max_overlap_sec)
    return [row["candidate"] for row in selected]


def schedule_candidates_by_buffer(
    candidates: Sequence[_LegalCandidate],
    *,
    max_overlap_sec: float = 0.0,
) -> list[_LegalCandidate]:
    grouped: dict[tuple[str, str], list[_LegalCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.recording_id, candidate.buffer_id)].append(candidate)
    selected: list[_LegalCandidate] = []
    for key in sorted(grouped):
        selected.extend(weighted_interval_schedule(grouped[key], max_overlap_sec=max_overlap_sec))
    return selected


def clips_from_candidates(candidates: Sequence[_LegalCandidate]) -> list[VrPackedClip]:
    clips: list[VrPackedClip] = []
    for clip_index, candidate in enumerate(candidates):
        clips.append(
            VrPackedClip(
                detector_name=DETECTOR_NAME,
                packer_name=PACKER_NAME,
                source_id=candidate.source_id,
                recording_id=candidate.recording_id,
                buffer_id=candidate.buffer_id,
                clip_id=f"{DETECTOR_NAME}-{PACKER_NAME}-clip-{clip_index:06d}",
                start_sec=candidate.start_sec,
                end_sec=candidate.end_sec,
                duration_sec=candidate.duration_sec,
                start_cutpoint_id=candidate.start_cutpoint_id,
                end_cutpoint_id=candidate.end_cutpoint_id,
                selected_weight=candidate.weight,
            )
        )
    return clips


def cutpoints_used_by_selected_schedule(
    cutpoints: Sequence[VrCutpoint],
    selected: Sequence[_LegalCandidate],
) -> tuple[VrCutpoint, ...]:
    selected_ids = {candidate.start_cutpoint_id for candidate in selected} | {
        candidate.end_cutpoint_id for candidate in selected
    }
    chosen = tuple(cut for cut in cutpoints if cut.cutpoint_id in selected_ids)
    found_ids = {cut.cutpoint_id for cut in chosen}
    missing = selected_ids - found_ids
    if missing:
        preview = ", ".join(sorted(missing)[:8])
        raise RuntimeError(
            "selected schedule references cutpoint ids missing from the detector pool: "
            f"{preview}"
        )
    return chosen


def slice_wav(
    audio_path: Path,
    *,
    recording_id: str,
    sample_rate_hz: int,
    buffers: Sequence[BufferScope],
    source_id: str | None = None,
    geometry: VrGeometry = VR_O0_4,
) -> VrSlicerResult:
    import soundfile as sf

    fingerprint = assert_locked_geometry(geometry)
    if int(sample_rate_hz) != int(geometry.sample_rate_hz):
        raise ValueError(
            f"request sample_rate_hz {sample_rate_hz} != VR geometry {geometry.sample_rate_hz}"
        )
    if not buffers:
        raise ValueError("VR slicer buffers must be non-empty")
    samples, actual_rate = sf.read(str(audio_path), dtype="float32", always_2d=False)
    if int(actual_rate) != int(sample_rate_hz):
        raise ValueError(f"audio sample rate {actual_rate} != request {sample_rate_hz}")
    resolved_source_id = source_id or f"source_{recording_id}"
    vad_frames = compute_vad_frames(samples, int(actual_rate), geometry)
    frames = attach_rms_voicing(vad_frames, samples, int(actual_rate))
    cutpoints = detect_cutpoints(
        frames=frames,
        buffers=buffers,
        source_id=resolved_source_id,
        recording_id=recording_id,
        geometry=geometry,
    )
    legal = generate_legal_candidates(cutpoints, geometry)
    selected = schedule_candidates_by_buffer(legal, max_overlap_sec=0.0)
    clips = clips_from_candidates(selected)
    selected_cuts = cutpoints_used_by_selected_schedule(cutpoints, selected)
    if clips and not selected_cuts:
        raise RuntimeError("emitted clips but selected schedule has no cutpoints")
    return VrSlicerResult(
        geometry_fingerprint=fingerprint,
        cutpoints=tuple(cutpoints),
        clips=tuple(clips),
        selected_cutpoints=selected_cuts,
    )
