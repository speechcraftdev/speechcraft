"""Explicit immutable geometry configs.

A and D share one execution path; they differ only by these values.
Phase 7 adds named overlap/spacing variants with the same detector/packer knobs.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


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
PHASE7_GEOMETRIES_BY_NAME: dict[str, GeometryConfig] = {
    config.name: config for config in (*PHASE7_GEOMETRIES, O12_5_4, O0_2)
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
