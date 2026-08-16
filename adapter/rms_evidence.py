"""Fine-scale raw-waveform RMS helpers for Phase-8 round 2.

These operate on 5–10 ms hops of the waveform, not the 32 ms VAD-frame RMS
attached to Silero windows. Recording-relative silence uses a low percentile
of that fine grid, not a 75th-percentile “speech reference.”
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from adapter.config import RmsPolicySpec
from adapter.feature_bundle import FeatureBundle


def rms_dbfs_from_samples(samples: Sequence[float]) -> float:
    n = len(samples)
    if n == 0:
        return -120.0
    try:
        import numpy as np

        chunk = np.asarray(samples, dtype=np.float64)
        mean_sq = float(np.mean(np.square(chunk)))
    except ImportError:
        mean_sq = sum(float(x) * float(x) for x in samples) / float(n)
    rms = math.sqrt(mean_sq)
    if rms <= 1e-8:
        return -120.0
    return 20.0 * math.log10(rms)


def slice_by_time(
    times_sec: Sequence[float],
    values: Sequence[float],
    start_sec: float,
    end_sec: float,
) -> tuple[float, ...]:
    if end_sec <= start_sec:
        return ()
    return tuple(
        float(value)
        for time_sec, value in zip(times_sec, values)
        if start_sec <= float(time_sec) < end_sec
    )


def mean_or(values: Sequence[float], default: float | None) -> float | None:
    if not values:
        return default
    return sum(float(v) for v in values) / float(len(values))


def percentile(values: Sequence[float], p: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return -120.0
    if p <= 0.0:
        return ordered[0]
    if p >= 100.0:
        return ordered[-1]
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    idx = min(len(ordered) - 1, max(0, idx))
    return ordered[idx]


def recording_silence_threshold(rms_dbfs: Sequence[float], spec: RmsPolicySpec) -> float:
    """Quiet-end threshold: low percentile of fine RMS, plus an optional margin."""
    return percentile(rms_dbfs, spec.silence_percentile) + spec.silence_margin_db


def clip_interval(
    start_sec: float,
    end_sec: float,
    bound_start: float,
    bound_end: float,
) -> tuple[float, float]:
    lo = max(float(start_sec), float(bound_start))
    hi = min(float(end_sec), float(bound_end))
    return lo, hi


def placement_grid(
    *,
    audio: Sequence[float],
    sample_rate_hz: int,
    start_sec: float,
    end_sec: float,
    window_ms: float,
    hop_ms: float,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """RMS grid that never reads outside [start_sec, end_sec]."""
    if end_sec <= start_sec:
        return (), ()
    sr = int(sample_rate_hz)
    start_i = max(0, int(round(start_sec * sr)))
    end_i = min(len(audio), int(round(end_sec * sr)))
    win = max(1, int(round(window_ms * sr / 1000.0)))
    hop = max(1, int(round(hop_ms * sr / 1000.0)))
    if end_i - start_i < 1:
        return (), ()
    times: list[float] = []
    values: list[float] = []
    index = start_i
    while index < end_i:
        chunk_end = min(end_i, index + win)
        center = (index + chunk_end) / (2.0 * sr)
        values.append(rms_dbfs_from_samples(audio[index:chunk_end]))
        times.append(center)
        if index + hop >= end_i:
            break
        index += hop
    return tuple(times), tuple(values)


@dataclass(frozen=True)
class FineRmsGrid:
    """Immutable 5–10 ms waveform RMS. Centers are inside allowed buffers only."""

    times_sec: tuple[float, ...]
    rms_dbfs: tuple[float, ...]
    window_ms: float
    hop_ms: float


def build_fine_rms_grid(bundle: FeatureBundle, spec: RmsPolicySpec) -> FineRmsGrid:
    """One per-recording grid: concatenate per-buffer windows, never read gaps."""
    times: list[float] = []
    values: list[float] = []
    sr = int(bundle.sample_rate_hz)
    for buf in bundle.buffers:
        buf_times, buf_rms = placement_grid(
            audio=bundle.audio,
            sample_rate_hz=sr,
            start_sec=float(buf.start_sec),
            end_sec=float(buf.end_sec),
            window_ms=spec.fine_window_ms,
            hop_ms=spec.fine_hop_ms,
        )
        times.extend(buf_times)
        values.extend(buf_rms)
    return FineRmsGrid(
        times_sec=tuple(times),
        rms_dbfs=tuple(values),
        window_ms=float(spec.fine_window_ms),
        hop_ms=float(spec.fine_hop_ms),
    )


def require_grid_spec(grid: FineRmsGrid, spec: RmsPolicySpec) -> None:
    if grid.window_ms != float(spec.fine_window_ms) or grid.hop_ms != float(spec.fine_hop_ms):
        raise RuntimeError(
            "fine RMS grid mismatch: "
            f"grid window/hop={grid.window_ms}/{grid.hop_ms} "
            f"!= spec {spec.fine_window_ms}/{spec.fine_hop_ms}"
        )


def _bounded_slice(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    start_sec: float,
    end_sec: float,
    bound_start: float,
    bound_end: float,
) -> tuple[float, ...]:
    lo, hi = clip_interval(start_sec, end_sec, bound_start, bound_end)
    return slice_by_time(times_sec, rms_dbfs, lo, hi)


def recording_speech_ref(rms_dbfs: Sequence[float], spec: RmsPolicySpec) -> float:
    """Typical-speech energy: high percentile of the fine RMS grid."""
    return percentile(rms_dbfs, spec.speech_percentile)


def context_speech_ref(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
    recording_ref: float,
    *,
    bound_start: float,
    bound_end: float,
) -> float:
    """Local outer-ring speech, else recording typical-speech.

    Inner shoulders (80 ms) can sit inside a long pause. The outer ring
    (shoulder..outer) is used only when it is actually louder than the center.
    All lookups are clipped to the candidate's allowed buffer.
    """
    half = spec.center_ms / 2000.0
    inner = spec.shoulder_ms / 1000.0
    outer = spec.outer_ms / 1000.0
    center = mean_or(
        _bounded_slice(times_sec, rms_dbfs, t_sec - half, t_sec + half, bound_start, bound_end),
        None,
    )
    outer_vals = _bounded_slice(
        times_sec, rms_dbfs, t_sec - outer, t_sec - inner, bound_start, bound_end
    ) + _bounded_slice(
        times_sec, rms_dbfs, t_sec + inner, t_sec + outer, bound_start, bound_end
    )
    outer_db = mean_or(outer_vals, None)
    if center is None or outer_db is None:
        return recording_ref
    if float(outer_db) - float(center) >= spec.outer_contrast_db:
        return float(outer_db)
    return recording_ref


def evidence_depth_db(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
    recording_ref: float,
    *,
    bound_start: float,
    bound_end: float,
) -> float | None:
    """Drop from speech context into the fine-scale center, in dB."""
    half = spec.center_ms / 2000.0
    center = mean_or(
        _bounded_slice(times_sec, rms_dbfs, t_sec - half, t_sec + half, bound_start, bound_end),
        None,
    )
    if center is None:
        return None
    speech_ref = context_speech_ref(
        times_sec,
        rms_dbfs,
        t_sec,
        spec,
        recording_ref,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    return float(speech_ref) - float(center)


def half_depth_width_ms(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
    recording_ref: float,
    *,
    bound_start: float,
    bound_end: float,
) -> float:
    """Width of the depression at half its depth vs speech context."""
    half = spec.center_ms / 2000.0
    center = mean_or(
        _bounded_slice(times_sec, rms_dbfs, t_sec - half, t_sec + half, bound_start, bound_end),
        None,
    )
    depth = evidence_depth_db(
        times_sec,
        rms_dbfs,
        t_sec,
        spec,
        recording_ref,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    if center is None or depth is None or depth <= 0.0:
        return 0.0
    threshold = float(center) + 0.5 * float(depth)
    return valley_width_below_threshold_ms(
        times_sec,
        rms_dbfs,
        t_sec,
        threshold,
        bound_start=bound_start,
        bound_end=bound_end,
    )


def valley_width_below_threshold_ms(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    threshold_db: float,
    *,
    bound_start: float,
    bound_end: float,
) -> float:
    """Contiguous fine-hop run around t, stopped at the allowed buffer."""
    in_bounds = [
        i
        for i, time_sec in enumerate(times_sec)
        if bound_start <= float(time_sec) < bound_end
    ]
    if not in_bounds:
        return 0.0
    nearest = min(in_bounds, key=lambda i: abs(float(times_sec[i]) - t_sec))
    if float(rms_dbfs[nearest]) > threshold_db:
        return 0.0
    lo = nearest
    while lo > 0 and bound_start <= float(times_sec[lo - 1]) < bound_end and float(rms_dbfs[lo - 1]) <= threshold_db:
        lo -= 1
    hi = nearest
    while (
        hi + 1 < len(times_sec)
        and bound_start <= float(times_sec[hi + 1]) < bound_end
        and float(rms_dbfs[hi + 1]) <= threshold_db
    ):
        hi += 1
    return max(0.0, (float(times_sec[hi]) - float(times_sec[lo])) * 1000.0)


def short_shallow_penalty(
    width_ms: float,
    depth_db: float | None,
    spec: RmsPolicySpec,
) -> float:
    """Zero unless the valley is both short and shallow."""
    if depth_db is None:
        return 0.0
    shortness = max(0.0, (spec.short_valley_ms - float(width_ms)) / spec.short_valley_ms)
    shallowness = max(
        0.0, (spec.shallow_depth_db - float(depth_db)) / spec.shallow_depth_db
    )
    return spec.score_scale * shortness * shallowness


def silence_ratio(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
    *,
    bound_start: float,
    bound_end: float,
    threshold_db: float,
) -> float:
    """Fraction of fine hops in ±context_ms that are at or below threshold."""
    half = spec.context_ms / 1000.0
    start_sec, end_sec = clip_interval(
        t_sec - half, t_sec + half, bound_start, bound_end
    )
    window = slice_by_time(times_sec, rms_dbfs, start_sec, end_sec)
    if not window:
        return 0.0
    quiet = sum(1 for value in window if float(value) <= threshold_db)
    return quiet / float(len(window))
