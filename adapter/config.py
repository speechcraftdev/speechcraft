"""Explicit immutable A / D geometry configs for Phase 2.

A and D share one execution path; they differ only by these values.
"""

from __future__ import annotations

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
