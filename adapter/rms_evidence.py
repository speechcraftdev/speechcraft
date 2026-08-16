"""Fixed-window RMS/pause evidence helpers. Temporary Phase-8 experiment code."""

from __future__ import annotations

import math
from collections.abc import Sequence

from adapter.config import RmsPolicySpec


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


def mean_or(values: Sequence[float], default: float) -> float:
    if not values:
        return default
    return sum(float(v) for v in values) / float(len(values))


def center_and_shoulders(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> tuple[float, float, float]:
    """Return (center_db, left_shoulder_db, right_shoulder_db)."""
    half = spec.center_ms / 2000.0
    shoulder = spec.shoulder_ms / 1000.0
    center = mean_or(slice_by_time(times_sec, rms_dbfs, t_sec - half, t_sec + half), -120.0)
    left = mean_or(
        slice_by_time(times_sec, rms_dbfs, t_sec - half - shoulder, t_sec - half),
        center,
    )
    right = mean_or(
        slice_by_time(times_sec, rms_dbfs, t_sec + half, t_sec + half + shoulder),
        center,
    )
    return center, left, right


def prominence_drops_db(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> tuple[float, float]:
    center, left, right = center_and_shoulders(times_sec, rms_dbfs, t_sec, spec)
    return left - center, right - center


def prominence_score(left_drop_db: float, right_drop_db: float) -> float:
    """Both-sided valley strength. Average of drops; peaks score negative."""
    return 0.5 * (float(left_drop_db) + float(right_drop_db))


def bilateral_score(left_drop_db: float, right_drop_db: float) -> float:
    """One-sided valleys are weak: the weaker side dominates."""
    return min(float(left_drop_db), float(right_drop_db))


def long_scale_db(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> float:
    half = spec.long_ms / 2000.0
    return mean_or(slice_by_time(times_sec, rms_dbfs, t_sec - half, t_sec + half), -120.0)


def multiscale_pause_score(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> float:
    """Long-context quietness relative to short-center energy.

    A brief consonant dip is low on the short window but the long window stays
    loud, so this is near zero or negative. A sustained pause is low on both,
    and the surrounding shoulders still make the long window quieter than speech.
    """
    center, left, right = center_and_shoulders(times_sec, rms_dbfs, t_sec, spec)
    long_db = long_scale_db(times_sec, rms_dbfs, t_sec, spec)
    speech_ref = 0.5 * (left + right)
    long_quiet = speech_ref - long_db
    short_quiet = speech_ref - center
    # Require the longer context to look like a pause, not only the 24 ms dip.
    return min(short_quiet, long_quiet)


def valley_run_indices(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> tuple[int, int] | None:
    if not times_sec:
        return None
    speech_ref = sorted(float(v) for v in rms_dbfs)[int(0.75 * (len(rms_dbfs) - 1))]
    threshold = speech_ref - spec.valley_margin_db
    nearest = min(range(len(times_sec)), key=lambda i: abs(float(times_sec[i]) - t_sec))
    if float(rms_dbfs[nearest]) > threshold:
        return None
    lo = nearest
    while lo > 0 and float(rms_dbfs[lo - 1]) <= threshold:
        lo -= 1
    hi = nearest
    while hi + 1 < len(times_sec) and float(rms_dbfs[hi + 1]) <= threshold:
        hi += 1
    return lo, hi


def valley_width_ms(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> float:
    """Duration of the contiguous low-energy run around t. Not a hard gate."""
    bounds = valley_run_indices(times_sec, rms_dbfs, t_sec, spec)
    if bounds is None:
        return 0.0
    lo, hi = bounds
    return max(0.0, (float(times_sec[hi]) - float(times_sec[lo])) * 1000.0)


def valley_shape_score(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    t_sec: float,
    spec: RmsPolicySpec,
) -> float:
    """Soft wider+deeper is better. Shoulders are taken outside the valley run."""
    bounds = valley_run_indices(times_sec, rms_dbfs, t_sec, spec)
    if bounds is None:
        return 0.0
    lo, hi = bounds
    center = mean_or(tuple(float(rms_dbfs[i]) for i in range(lo, hi + 1)), -120.0)
    run_start = float(times_sec[lo])
    run_end = float(times_sec[hi])
    shoulder = spec.shoulder_ms / 1000.0
    left = mean_or(
        slice_by_time(times_sec, rms_dbfs, run_start - shoulder, run_start),
        center,
    )
    right = mean_or(
        slice_by_time(times_sec, rms_dbfs, run_end, run_end + shoulder),
        center,
    )
    depth = 0.5 * ((left - center) + (right - center))
    width = max(0.0, (run_end - run_start) * 1000.0)
    return depth + (width / spec.valley_width_ref_ms)


def argmin_in_bounds(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    start_sec: float,
    end_sec: float,
) -> float | None:
    """Return the time of the lowest RMS strictly inside [start, end]."""
    best_t: float | None = None
    best_rms = math.inf
    for time_sec, value in zip(times_sec, rms_dbfs):
        t = float(time_sec)
        if t < start_sec or t > end_sec:
            continue
        rms = float(value)
        if rms < best_rms or (rms == best_rms and best_t is not None and t < best_t):
            best_rms = rms
            best_t = t
    return best_t


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
