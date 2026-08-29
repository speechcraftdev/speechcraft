"""Explicit immutable geometry configs.

A and D share one execution path; they differ only by these values.
Phase 7 adds named overlap/spacing variants with the same detector/packer knobs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class RmsPolicySpec:
    """RMS veto/penalty knobs. Not part of the geometry fingerprint.

    Fingerprints include only knobs that affect the named kind. Fine-scale
    windows interrogate the raw waveform; they are not 32 ms VAD-frame RMS.
    """

    kind: str
    # Fine-scale raw-waveform interrogation (Linus: 5–10 ms hops).
    fine_window_ms: float = 10.0
    fine_hop_ms: float = 8.0
    # Half-width around the cut for silence-ratio (±context).
    context_ms: float = 64.0
    center_ms: float = 20.0
    shoulder_ms: float = 80.0
    # Outer ring for local speech context when inner shoulders sit in a pause.
    outer_ms: float = 250.0
    outer_contrast_db: float = 1.5
    # Weak-valley veto: reject if depth vs speech context is below this.
    veto_depth_db: float = 1.5
    # Short+shallow penalty: short is not enough; short AND shallow is.
    short_valley_ms: float = 64.0
    shallow_depth_db: float = 6.0
    score_scale: float = 0.25
    # Typical-speech fallback for depth/width when the local outer ring is quiet.
    speech_percentile: float = 75.0
    # Recording-relative silence threshold: quiet-end percentile of fine RMS.
    # Margin 0 keeps the threshold on the quiet tail instead of lifting it into
    # speech (the round-1 valley-shape mistake).
    silence_percentile: float = 10.0
    silence_margin_db: float = 0.0


def rms_policy_payload(spec: RmsPolicySpec) -> dict[str, object]:
    """Behavior-producing knobs only. Unused fields must not enter fingerprints."""
    if spec.kind == "current":
        return {"kind": "current"}
    grid = {
        "kind": spec.kind,
        "fine_window_ms": float(spec.fine_window_ms),
        "fine_hop_ms": float(spec.fine_hop_ms),
    }
    if spec.kind == "weak_valley_veto":
        return {
            **grid,
            "center_ms": float(spec.center_ms),
            "shoulder_ms": float(spec.shoulder_ms),
            "outer_ms": float(spec.outer_ms),
            "outer_contrast_db": float(spec.outer_contrast_db),
            "veto_depth_db": float(spec.veto_depth_db),
            "speech_percentile": float(spec.speech_percentile),
        }
    if spec.kind == "short_shallow_penalty":
        return {
            **grid,
            "center_ms": float(spec.center_ms),
            "shoulder_ms": float(spec.shoulder_ms),
            "outer_ms": float(spec.outer_ms),
            "outer_contrast_db": float(spec.outer_contrast_db),
            "short_valley_ms": float(spec.short_valley_ms),
            "shallow_depth_db": float(spec.shallow_depth_db),
            "speech_percentile": float(spec.speech_percentile),
            "score_scale": float(spec.score_scale),
        }
    if spec.kind == "waveform_silence_ratio":
        return {
            **grid,
            "context_ms": float(spec.context_ms),
            "score_scale": float(spec.score_scale),
            "silence_percentile": float(spec.silence_percentile),
            "silence_margin_db": float(spec.silence_margin_db),
        }
    raise RuntimeError(f"unknown rms_policy.kind {spec.kind!r}")


RMS_CURRENT = RmsPolicySpec(kind="current")
RMS_WEAK_VALLEY_VETO = RmsPolicySpec(kind="weak_valley_veto")
RMS_SHORT_SHALLOW_PENALTY = RmsPolicySpec(kind="short_shallow_penalty")
RMS_WAVEFORM_SILENCE_RATIO = RmsPolicySpec(kind="waveform_silence_ratio")

# Frozen 15-feature waveform-first schema. Order is part of the policy fingerprint.
# RMS fields are FineRmsGrid dBFS, not linear amplitude:
#   center_rms / left_rms / right_rms: dBFS
#   rms_asymmetry: left_rms - right_rms (dB). Positive = left louder than right.
FEATURE_NAMES: tuple[str, ...] = (
    "center_rms",
    "rms_percentile",
    "rms_valley_depth",
    "rms_valley_width",
    "left_rms",
    "right_rms",
    "rms_asymmetry",
    "zcr_40ms",
    "zcr_80ms",
    "abs_amplitude_p90",
    "short_window_energy_variance",
    "spectral_flatness",
    "high_frequency_energy_fraction",
    "vad_mean_64ms",
    "low_vad_run_width",
)
RMS_WAVEFORM = FEATURE_NAMES[:13]
VAD_ONLY = FEATURE_NAMES[13:]
ALL = FEATURE_NAMES
LOGISTIC_BOUNDARY_FEATURE_NAMES = FEATURE_NAMES
FEATURE_SCHEMA_ID = "o0_4_boundary_features_v1"
FEATURE_SUBSET_ALL = "all"
FEATURE_SUBSET_RMS_WAVEFORM = "rms_waveform"
FEATURE_SUBSET_VAD_ONLY = "vad_only"
PHASE10B_TARGETS: tuple[str, ...] = ("inside", "gt20ms", "gt50ms")
PHASE10B_FEATURE_SETS: tuple[str, ...] = (
    FEATURE_SUBSET_RMS_WAVEFORM,
    FEATURE_SUBSET_VAD_ONLY,
    FEATURE_SUBSET_ALL,
)
PHASE10B_N_SPLITS = 5


def feature_names_for_subset(subset: str) -> tuple[str, ...]:
    if subset == FEATURE_SUBSET_ALL:
        return ALL
    if subset == FEATURE_SUBSET_RMS_WAVEFORM:
        return RMS_WAVEFORM
    if subset == FEATURE_SUBSET_VAD_ONLY:
        return VAD_ONLY
    raise RuntimeError(f"unknown logistic feature_subset {subset!r}")


@dataclass(frozen=True)
class LogisticSpec:
    """P(bad boundary) scorer. Not part of the geometry fingerprint."""

    kind: str
    model_id: str
    target: str
    feature_subset: str
    feature_names: tuple[str, ...]
    feature_mean: tuple[float, ...]
    feature_scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float = 0.0
    score_scale: float = 1.0

    def __post_init__(self) -> None:
        for name, values in (
            ("feature_mean", self.feature_mean),
            ("feature_scale", self.feature_scale),
            ("coefficients", self.coefficients),
        ):
            for index, value in enumerate(values):
                if not math.isfinite(float(value)):
                    raise RuntimeError(f"non-finite logistic {name}[{index}]: {value}")
                if name == "feature_scale" and float(value) <= 0.0:
                    raise RuntimeError("logistic feature_scale must be strictly positive")
        if not math.isfinite(float(self.intercept)):
            raise RuntimeError(f"non-finite logistic intercept: {self.intercept}")
        if not math.isfinite(float(self.score_scale)):
            raise RuntimeError(f"non-finite logistic score_scale: {self.score_scale}")


def logistic_policy_payload(spec: LogisticSpec) -> dict[str, object]:
    """Behavior-producing knobs only."""
    if spec.kind != "boundary_v1":
        raise RuntimeError(f"unknown logistic.kind {spec.kind!r}")
    if spec.target != "p_bad":
        raise RuntimeError(f"logistic.target must be 'p_bad', got {spec.target!r}")
    return {
        "kind": spec.kind,
        "model_id": str(spec.model_id),
        "target": str(spec.target),
        "feature_subset": str(spec.feature_subset),
        "feature_names": [str(name) for name in spec.feature_names],
        "feature_mean": [float(value) for value in spec.feature_mean],
        "feature_scale": [float(value) for value in spec.feature_scale],
        "coefficients": [float(value) for value in spec.coefficients],
        "intercept": float(spec.intercept),
        "score_scale": float(spec.score_scale),
    }


def untrained_logistic_spec(subset: str = FEATURE_SUBSET_ALL) -> LogisticSpec:
    names = feature_names_for_subset(subset)
    n = len(names)
    return LogisticSpec(
        kind="boundary_v1",
        model_id="untrained_zero",
        target="p_bad",
        feature_subset=subset,
        feature_names=names,
        feature_mean=(0.0,) * n,
        feature_scale=(1.0,) * n,
        coefficients=(0.0,) * n,
        intercept=0.0,
        score_scale=1.0,
    )


LOGISTIC_BOUNDARY_V1_UNTRAINED = untrained_logistic_spec(FEATURE_SUBSET_ALL)


@dataclass(frozen=True)
class GeometryConfig:
    """Minimal geometry + detector knobs for vad_percentile_rms baselines.

    Detector fields match speaker_ts_eval BenchmarkConfig defaults required to
    reproduce the existing vad_percentile_rms baseline; this is not a general
    purpose config model.
    """

    name: str
    window_samples: int
    hop_samples: int
    offsets: tuple[int, ...]
    sample_rate_hz: int = 16000
    # Faithful vad_percentile_rms defaults from speaker_ts_eval.
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
    # Policy knobs. Not part of the geometry fingerprint.
    min_quiet_run_ms: float | None = None
    scoring: str | None = None
    rms_policy: RmsPolicySpec | None = None
    logistic: LogisticSpec | None = None


# Current overlapping geometry (validated A baseline).
CURRENT_A = GeometryConfig(
    name="current_A",
    window_samples=512,
    hop_samples=256,
    offsets=(0, 128),
    sample_rate_hz=16000,
)

# Proper four-stream geometry (proper D baseline).
PROPER_D = GeometryConfig(
    name="proper_D",
    window_samples=512,
    hop_samples=512,
    offsets=(0, 128, 256, 384),
    sample_rate_hz=16000,
)

# A-geometry, 64 ms minimum quiet-run gate on detector candidates.
MIN_QUIET_RUN_64MS = GeometryConfig(
    name="min_quiet_run_64ms",
    window_samples=512,
    hop_samples=256,
    offsets=(0, 128),
    sample_rate_hz=16000,
    min_quiet_run_ms=64.0,
)

# A-geometry, quiet-evidence rescoring of candidates and packer weights.
QUIET_RUN_SCORE = GeometryConfig(
    name="quiet_run_score",
    window_samples=512,
    hop_samples=256,
    offsets=(0, 128),
    sample_rate_hz=16000,
    scoring="quiet_evidence",
)

PHASE6_CONTENDERS: tuple[GeometryConfig, ...] = (
    CURRENT_A,
    PROPER_D,
    MIN_QUIET_RUN_64MS,
    QUIET_RUN_SCORE,
)

# Phase 7 geometry-only tournament. Same detector/RMS/packer as A/D; names encode
# recurrent overlap and aggregate observation spacing. CURRENT_A / PROPER_D keep
# their historical names so Phase 4–6 scripts stay bit-identical.
A_O50_8 = GeometryConfig(
    name="A_O50_8",
    window_samples=512,
    hop_samples=256,
    offsets=(0, 128),
    sample_rate_hz=16000,
)
D_O0_8 = GeometryConfig(
    name="D_O0_8",
    window_samples=512,
    hop_samples=512,
    offsets=(0, 128, 256, 384),
    sample_rate_hz=16000,
)
O75_8 = GeometryConfig(
    name="O75_8",
    window_samples=512,
    hop_samples=128,
    offsets=(0,),
    sample_rate_hz=16000,
)
O25_8 = GeometryConfig(
    name="O25_8",
    window_samples=512,
    hop_samples=384,
    offsets=(0, 128, 256),
    sample_rate_hz=16000,
)
O50_4 = GeometryConfig(
    name="O50_4",
    window_samples=512,
    hop_samples=256,
    offsets=(0, 64, 128, 192),
    sample_rate_hz=16000,
)
O25_4 = GeometryConfig(
    name="O25_4",
    window_samples=512,
    hop_samples=384,
    offsets=(0, 64, 128, 192, 256, 320),
    sample_rate_hz=16000,
)
O0_4 = GeometryConfig(
    name="O0_4",
    window_samples=512,
    hop_samples=512,
    offsets=(0, 64, 128, 192, 256, 320, 384, 448),
    sample_rate_hz=16000,
)
# O0_4 geometry with an opt-in logistic scorer. Frozen O0_4 stays scoring=None.
O0_4_LOGISTIC = GeometryConfig(
    name="O0_4_LOGISTIC",
    window_samples=512,
    hop_samples=512,
    offsets=(0, 64, 128, 192, 256, 320, 384, 448),
    sample_rate_hz=16000,
    scoring="logistic_boundary",
    logistic=LOGISTIC_BOUNDARY_V1_UNTRAINED,
)
PHASE10_LOGISTIC_CONTENDERS: tuple[GeometryConfig, ...] = (O0_4, O0_4_LOGISTIC)
# 12.5% recurrent overlap, ~4 ms aggregate spacing. Between O25_4 and O0_4.
O12_5_4 = GeometryConfig(
    name="O12.5_4",
    window_samples=512,
    hop_samples=448,
    offsets=(0, 64, 128, 192, 256, 320, 384),
    sample_rate_hz=16000,
)
# Proper non-overlapping recurrence, ~2 ms aggregate spacing.
O0_2 = GeometryConfig(
    name="O0_2",
    window_samples=512,
    hop_samples=512,
    offsets=tuple(range(0, 512, 32)),
    sample_rate_hz=16000,
)

PHASE7_GEOMETRIES: tuple[GeometryConfig, ...] = (
    A_O50_8,
    D_O0_8,
    O75_8,
    O25_8,
    O50_4,
    O25_4,
    O0_4,
)
# Post-smoke full-run set. Not another 7-way sweep.
PHASE7_FINALISTS: tuple[GeometryConfig, ...] = (
    A_O50_8,
    D_O0_8,
    O25_4,
    O0_4,
    O12_5_4,
    O0_2,
)

def _o25_rms(name: str, spec: RmsPolicySpec) -> GeometryConfig:
    """O25_4 geometry with a different RMS/evidence policy only."""
    return replace(O25_4, name=name, rms_policy=spec)


O25_4_CURRENT = _o25_rms("O25_4_CURRENT", RMS_CURRENT)
O25_4_WEAK_VALLEY_VETO = _o25_rms("O25_4_WEAK_VALLEY_VETO", RMS_WEAK_VALLEY_VETO)
O25_4_SHORT_SHALLOW_PENALTY = _o25_rms(
    "O25_4_SHORT_SHALLOW_PENALTY", RMS_SHORT_SHALLOW_PENALTY
)
O25_4_WAVEFORM_SILENCE_RATIO = _o25_rms(
    "O25_4_WAVEFORM_SILENCE_RATIO", RMS_WAVEFORM_SILENCE_RATIO
)

# Round-2 set. The seven Phase-8 RMS scorers were dropped after smoke.
PHASE8_RMS_VARIANTS: tuple[GeometryConfig, ...] = (
    O25_4_CURRENT,
    O25_4_WEAK_VALLEY_VETO,
    O25_4_SHORT_SHALLOW_PENALTY,
    O25_4_WAVEFORM_SILENCE_RATIO,
)
PHASE8_SMOKE_CONTENDERS: tuple[GeometryConfig, ...] = (
    A_O50_8,
    *PHASE8_RMS_VARIANTS,
    O0_4,
    O0_2,
)
PHASE8_FROZEN_GEOMETRIES: tuple[GeometryConfig, ...] = (
    A_O50_8,
    O25_4,
    O0_4,
    O0_2,
)

PHASE7_GEOMETRIES_BY_NAME: dict[str, GeometryConfig] = {
    config.name: config
    for config in (
        *PHASE7_GEOMETRIES,
        O12_5_4,
        O0_2,
        *PHASE8_RMS_VARIANTS,
        O0_4_LOGISTIC,
    )
}

# RESET-8 (per-window Silero state reset at hop=128) is intentionally omitted.
# The canonical Silero path resets recurrent state once per offset stream, not
# per window. Per-window reset would need invasive custom Silero handling.


def recurrent_overlap_fraction(config: GeometryConfig) -> float:
    """Overlap inside one recurrent stream: 1 - hop/window, else 0."""
    if config.hop_samples >= config.window_samples:
        return 0.0
    return 1.0 - (float(config.hop_samples) / float(config.window_samples))


def aggregate_spacing_samples(config: GeometryConfig) -> int:
    """GCD of hop and nonzero offsets: aggregate observation spacing in samples."""
    values = [int(config.hop_samples), *[int(offset) for offset in config.offsets if offset]]
    spacing = values[0]
    for value in values[1:]:
        spacing = math.gcd(spacing, value)
    return spacing


def resolve_geometries(names: str | Sequence[str]) -> tuple[GeometryConfig, ...]:
    """Resolve a comma-separated or sequence of Phase-7 geometry names."""
    if isinstance(names, str):
        requested = tuple(part.strip() for part in names.split(",") if part.strip())
    else:
        requested = tuple(str(name) for name in names)
    if not requested:
        raise RuntimeError("geometry list is empty")
    unknown = [name for name in requested if name not in PHASE7_GEOMETRIES_BY_NAME]
    if unknown:
        raise RuntimeError(
            f"unknown geometry names {unknown}; known: {sorted(PHASE7_GEOMETRIES_BY_NAME)}"
        )
    return tuple(PHASE7_GEOMETRIES_BY_NAME[name] for name in requested)
