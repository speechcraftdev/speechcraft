"""Buffer-local 15-feature waveform/RMS/spectral/VAD extraction for logistic scoring.

Never reads evaluator annotations. Waveform, RMS, spectral, and VAD context
are clipped to the candidate's allowed buffer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from adapter.config import (
    ALL,
    FEATURE_NAMES,
    RmsPolicySpec,
    feature_names_for_subset,
)
from adapter.feature_bundle import FeatureBundle, buffer_bounds, require_bundle_geometry
from adapter.rms_evidence import (
    FineRmsGrid,
    build_fine_rms_grid,
    clip_interval,
    evidence_depth_db,
    half_depth_width_ms,
    mean_or,
    percentile,
    recording_speech_ref,
    slice_by_time,
)

assert len(FEATURE_NAMES) == 15
assert FEATURE_NAMES is ALL or FEATURE_NAMES == ALL

CENTER_MS = 20.0
SIDE_MS = 40.0
ZCR_40_MS = 40.0
ZCR_80_MS = 80.0
AMP_WINDOW_MS = 80.0
ENERGY_WINDOW_MS = 80.0
ENERGY_FRAME_MS = 10.0
SPECTRAL_WINDOW_MS = 40.0
HIGH_FREQ_HZ = 4000.0
VAD_MEAN_MS = 64.0
EMPTY_RMS_DB = -120.0
SPECTRAL_EPS = 1e-12
LOGISTIC_FINE_WINDOW_MS = 10.0
LOGISTIC_FINE_HOP_MS = 8.0

LOGISTIC_FINE_RMS_SPEC = RmsPolicySpec(
    kind="weak_valley_veto",
    fine_window_ms=LOGISTIC_FINE_WINDOW_MS,
    fine_hop_ms=LOGISTIC_FINE_HOP_MS,
)


def _sample_range(
    *,
    n_audio: int,
    sample_rate_hz: int,
    start_sec: float,
    end_sec: float,
    bound_start: float,
    bound_end: float,
) -> tuple[int, int]:
    lo, hi = clip_interval(start_sec, end_sec, bound_start, bound_end)
    start_i = max(0, int(round(lo * sample_rate_hz)))
    end_i = min(n_audio, int(round(hi * sample_rate_hz)))
    return start_i, end_i


def _samples(
    audio: Any,
    *,
    sample_rate_hz: int,
    start_sec: float,
    end_sec: float,
    bound_start: float,
    bound_end: float,
) -> tuple[float, ...]:
    start_i, end_i = _sample_range(
        n_audio=len(audio),
        sample_rate_hz=sample_rate_hz,
        start_sec=start_sec,
        end_sec=end_sec,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    if end_i <= start_i:
        return ()
    return tuple(float(value) for value in audio[start_i:end_i])


def _centered_samples(
    audio: Any,
    t_sec: float,
    window_ms: float,
    *,
    sample_rate_hz: int,
    bound_start: float,
    bound_end: float,
) -> tuple[float, ...]:
    half = window_ms / 2000.0
    return _samples(
        audio,
        sample_rate_hz=sample_rate_hz,
        start_sec=t_sec - half,
        end_sec=t_sec + half,
        bound_start=bound_start,
        bound_end=bound_end,
    )


def _buffer_hops(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    bound_start: float,
    bound_end: float,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    times: list[float] = []
    values: list[float] = []
    for time_sec, value in zip(times_sec, rms_dbfs):
        if bound_start <= float(time_sec) < bound_end:
            times.append(float(time_sec))
            values.append(float(value))
    return tuple(times), tuple(values)


def _side_rms(
    times_sec: Sequence[float],
    rms_dbfs: Sequence[float],
    start_sec: float,
    end_sec: float,
    bound_start: float,
    bound_end: float,
) -> float:
    lo, hi = clip_interval(start_sec, end_sec, bound_start, bound_end)
    window = slice_by_time(times_sec, rms_dbfs, lo, hi)
    found = mean_or(window, None)
    return EMPTY_RMS_DB if found is None else float(found)


def _rms_percentile(center_rms: float, buffer_rms: Sequence[float]) -> float:
    if not buffer_rms:
        return 0.0
    n_le = sum(1 for value in buffer_rms if float(value) <= float(center_rms))
    return 100.0 * n_le / float(len(buffer_rms))


def _zero_crossing_rate(samples: Sequence[float], sample_rate_hz: int) -> float:
    if len(samples) < 2:
        return 0.0
    crossings = 0
    prev = float(samples[0])
    for value in samples[1:]:
        current = float(value)
        if (prev >= 0.0) != (current >= 0.0):
            crossings += 1
        prev = current
    duration = (len(samples) - 1) / float(sample_rate_hz)
    if duration <= 0.0:
        return 0.0
    return crossings / duration


def _variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(float(v) for v in values) / float(len(values))
    return sum((float(v) - mean) ** 2 for v in values) / float(len(values))


def _frame_energies(
    samples: Sequence[float],
    sample_rate_hz: int,
    frame_ms: float,
) -> tuple[float, ...]:
    frame = max(1, int(round(frame_ms * sample_rate_hz / 1000.0)))
    energies: list[float] = []
    index = 0
    n = len(samples)
    while index + frame <= n:
        chunk = samples[index : index + frame]
        energies.append(sum(float(x) * float(x) for x in chunk) / float(len(chunk)))
        index += frame
    return tuple(energies)


def _hann(n: int) -> tuple[float, ...]:
    if n <= 0:
        return ()
    if n == 1:
        return (1.0,)
    return tuple(0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1)) for i in range(n))


def _hann_window(samples: Sequence[float]) -> tuple[float, ...]:
    window = _hann(len(samples))
    return tuple(float(sample) * weight for sample, weight in zip(samples, window))


def _rfft_power(samples: Sequence[float]) -> tuple[float, ...]:
    n = len(samples)
    if n == 0:
        return ()
    try:
        import numpy as np

        arr = np.asarray(samples, dtype=np.float64)
        spec = np.fft.rfft(arr)
        return tuple(float(value) for value in (spec.real * spec.real + spec.imag * spec.imag))
    except ImportError:
        powers: list[float] = []
        two_pi_n = 2.0 * math.pi / float(n)
        for k in range(n // 2 + 1):
            real = 0.0
            imag = 0.0
            angle_step = two_pi_n * k
            angle = 0.0
            for value in samples:
                real += float(value) * math.cos(angle)
                imag -= float(value) * math.sin(angle)
                angle += angle_step
            powers.append(real * real + imag * imag)
        return tuple(powers)


def _spectral_flatness(power: Sequence[float]) -> float:
    bins = [float(value) for value in power[1:]]
    if not bins:
        return 0.0
    shifted = [value + SPECTRAL_EPS for value in bins]
    log_mean = sum(math.log(value) for value in shifted) / float(len(shifted))
    arith = sum(shifted) / float(len(shifted))
    return math.exp(log_mean) / arith


def _high_frequency_fraction(power: Sequence[float], sample_rate_hz: int, n_samples: int) -> float:
    if not power or n_samples <= 0:
        return 0.0
    total = sum(float(value) for value in power)
    if total <= 0.0:
        return 0.0
    high = 0.0
    for k, value in enumerate(power):
        freq = k * float(sample_rate_hz) / float(n_samples)
        if freq >= HIGH_FREQ_HZ:
            high += float(value)
    return high / total


def _vad_in_buffer(
    centers_sec: Sequence[float],
    probs: Sequence[float],
    bound_start: float,
    bound_end: float,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    times: list[float] = []
    values: list[float] = []
    for time_sec, prob in zip(centers_sec, probs):
        if bound_start <= float(time_sec) < bound_end:
            times.append(float(time_sec))
            values.append(float(prob))
    return tuple(times), tuple(values)


def _vad_mean(
    centers_sec: Sequence[float],
    probs: Sequence[float],
    t_sec: float,
    bound_start: float,
    bound_end: float,
) -> float:
    half = VAD_MEAN_MS / 2000.0
    lo, hi = clip_interval(t_sec - half, t_sec + half, bound_start, bound_end)
    window = slice_by_time(centers_sec, probs, lo, hi)
    found = mean_or(window, None)
    return 0.0 if found is None else float(found)


def _low_vad_run_width_ms(
    centers_sec: Sequence[float],
    probs: Sequence[float],
    t_sec: float,
    threshold: float,
    bound_start: float,
    bound_end: float,
) -> float:
    times, values = _vad_in_buffer(centers_sec, probs, bound_start, bound_end)
    if not times:
        return 0.0
    nearest = min(range(len(times)), key=lambda i: abs(times[i] - t_sec))
    if values[nearest] >= threshold:
        return 0.0
    lo = nearest
    while lo > 0 and values[lo - 1] < threshold:
        lo -= 1
    hi = nearest
    while hi + 1 < len(values) and values[hi + 1] < threshold:
        hi += 1
    if hi + 1 < len(times):
        step = times[hi + 1] - times[hi]
    elif lo > 0:
        step = times[lo] - times[lo - 1]
    elif len(times) >= 2:
        step = times[1] - times[0]
    else:
        step = 0.0
    start_sec = max(bound_start, times[lo])
    end_sec = min(bound_end, times[hi] + max(0.0, step))
    return max(0.0, (end_sec - start_sec) * 1000.0)


def _require_finite_features(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != len(FEATURE_NAMES):
        raise RuntimeError(
            f"logistic feature length {len(values)} != {len(FEATURE_NAMES)}"
        )
    for name, value in zip(FEATURE_NAMES, values):
        if not math.isfinite(value):
            raise RuntimeError(f"non-finite logistic feature {name}: {value}")
    return values


def extract_all_boundary_features(
    *,
    audio: Any,
    sample_rate_hz: int,
    t_sec: float,
    bound_start: float,
    bound_end: float,
    fine_grid: FineRmsGrid,
    vad_centers_sec: Sequence[float],
    vad_speech_prob: Sequence[float],
    vad_threshold: float,
) -> tuple[float, ...]:
    """Return the frozen 15-vector. All lookups stop at the allowed buffer.

    center_rms / left_rms / right_rms are FineRmsGrid dBFS.
    rms_asymmetry is left_rms - right_rms in dB (positive: left louder).
    """
    times, rms = _buffer_hops(fine_grid.times_sec, fine_grid.rms_dbfs, bound_start, bound_end)
    half_center = CENTER_MS / 2000.0
    side = SIDE_MS / 1000.0
    center_vals = slice_by_time(times, rms, t_sec - half_center, t_sec + half_center)
    center_rms = EMPTY_RMS_DB if not center_vals else float(mean_or(center_vals, EMPTY_RMS_DB))
    left_rms = _side_rms(times, rms, t_sec - side, t_sec, bound_start, bound_end)
    right_rms = _side_rms(times, rms, t_sec, t_sec + side, bound_start, bound_end)
    speech_ref = recording_speech_ref(rms, LOGISTIC_FINE_RMS_SPEC) if rms else EMPTY_RMS_DB
    depth = evidence_depth_db(
        times,
        rms,
        t_sec,
        LOGISTIC_FINE_RMS_SPEC,
        speech_ref,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    width = half_depth_width_ms(
        times,
        rms,
        t_sec,
        LOGISTIC_FINE_RMS_SPEC,
        speech_ref,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    zcr40 = _zero_crossing_rate(
        _centered_samples(
            audio,
            t_sec,
            ZCR_40_MS,
            sample_rate_hz=sample_rate_hz,
            bound_start=bound_start,
            bound_end=bound_end,
        ),
        sample_rate_hz,
    )
    zcr80 = _zero_crossing_rate(
        _centered_samples(
            audio,
            t_sec,
            ZCR_80_MS,
            sample_rate_hz=sample_rate_hz,
            bound_start=bound_start,
            bound_end=bound_end,
        ),
        sample_rate_hz,
    )
    amp_samples = _centered_samples(
        audio,
        t_sec,
        AMP_WINDOW_MS,
        sample_rate_hz=sample_rate_hz,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    abs_p90 = 0.0 if not amp_samples else percentile(tuple(abs(x) for x in amp_samples), 90.0)
    energy_samples = _centered_samples(
        audio,
        t_sec,
        ENERGY_WINDOW_MS,
        sample_rate_hz=sample_rate_hz,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    energy_var = _variance(_frame_energies(energy_samples, sample_rate_hz, ENERGY_FRAME_MS))
    spectral_samples = _centered_samples(
        audio,
        t_sec,
        SPECTRAL_WINDOW_MS,
        sample_rate_hz=sample_rate_hz,
        bound_start=bound_start,
        bound_end=bound_end,
    )
    power = _rfft_power(_hann_window(spectral_samples))
    flatness = _spectral_flatness(power)
    hf_frac = _high_frequency_fraction(power, sample_rate_hz, len(spectral_samples))
    vad_times, vad_probs = _vad_in_buffer(
        vad_centers_sec, vad_speech_prob, bound_start, bound_end
    )
    return _require_finite_features(
        (
            float(center_rms),
            _rms_percentile(center_rms, rms),
            0.0 if depth is None else float(depth),
            float(width),
            float(left_rms),
            float(right_rms),
            float(left_rms) - float(right_rms),
            float(zcr40),
            float(zcr80),
            float(abs_p90),
            float(energy_var),
            float(flatness),
            float(hf_frac),
            _vad_mean(vad_times, vad_probs, t_sec, bound_start, bound_end),
            _low_vad_run_width_ms(
                vad_times, vad_probs, t_sec, vad_threshold, bound_start, bound_end
            ),
        )
    )


def extract_boundary_features(
    *,
    audio: Any,
    sample_rate_hz: int,
    t_sec: float,
    bound_start: float,
    bound_end: float,
    fine_grid: FineRmsGrid,
    vad_centers_sec: Sequence[float],
    vad_speech_prob: Sequence[float],
    vad_threshold: float,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
) -> tuple[float, ...]:
    values = extract_all_boundary_features(
        audio=audio,
        sample_rate_hz=sample_rate_hz,
        t_sec=t_sec,
        bound_start=bound_start,
        bound_end=bound_end,
        fine_grid=fine_grid,
        vad_centers_sec=vad_centers_sec,
        vad_speech_prob=vad_speech_prob,
        vad_threshold=vad_threshold,
    )
    by_name = dict(zip(FEATURE_NAMES, values))
    unknown = [name for name in feature_names if name not in by_name]
    if unknown:
        raise RuntimeError(f"unknown logistic features: {unknown}")
    return tuple(by_name[name] for name in feature_names)


def features_from_cut(
    cut: Any,
    bundle: FeatureBundle,
    fine_grid: FineRmsGrid | None = None,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
) -> tuple[float, ...]:
    bound_start, bound_end = buffer_bounds(bundle, str(cut.buffer_id))
    grid = fine_grid if fine_grid is not None else build_fine_rms_grid(bundle, LOGISTIC_FINE_RMS_SPEC)
    return extract_boundary_features(
        audio=bundle.audio,
        sample_rate_hz=int(bundle.sample_rate_hz),
        t_sec=float(cut.time_sec),
        bound_start=bound_start,
        bound_end=bound_end,
        fine_grid=grid,
        vad_centers_sec=bundle.vad_centers_sec,
        vad_speech_prob=bundle.vad_speech_prob,
        vad_threshold=0.2,
        feature_names=feature_names,
    )


def features_from_cut_for_config(
    cut: Any,
    bundle: FeatureBundle,
    config: Any,
    fine_grid: FineRmsGrid | None = None,
) -> tuple[float, ...]:
    require_bundle_geometry(bundle, config)
    spec = config.logistic
    names = spec.feature_names if spec is not None else feature_names_for_subset("all")
    bound_start, bound_end = buffer_bounds(bundle, str(cut.buffer_id))
    grid = fine_grid if fine_grid is not None else build_fine_rms_grid(bundle, LOGISTIC_FINE_RMS_SPEC)
    return extract_boundary_features(
        audio=bundle.audio,
        sample_rate_hz=int(bundle.sample_rate_hz),
        t_sec=float(cut.time_sec),
        bound_start=bound_start,
        bound_end=bound_end,
        fine_grid=grid,
        vad_centers_sec=bundle.vad_centers_sec,
        vad_speech_prob=bundle.vad_speech_prob,
        vad_threshold=float(config.vad_threshold),
        feature_names=names,
    )
