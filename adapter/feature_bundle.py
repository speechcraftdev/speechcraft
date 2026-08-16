"""Immutable per-recording acoustic bundle. No evaluator annotations."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from adapter.config import GeometryConfig
from adapter.diagnostics import geometry_fingerprint
from referee.types import BufferScope

_FORBIDDEN_BUNDLE_FIELDS = (
    "phones",
    "words",
    "uncertainty",
    "uncertainty_intervals",
    "reference",
    "annotations",
    "score",
    "metrics",
)


def _readonly_sequence(values: Any) -> Any:
    try:
        import numpy as np

        array = np.asarray(values)
        if array.size and array.flags.writeable:
            array = np.array(array, copy=True)
            array.setflags(write=False)
        elif array.flags.writeable:
            array.setflags(write=False)
        return array
    except ImportError:
        return tuple(values)


@dataclass(frozen=True)
class FeatureBundle:
    """Geometry-keyed VAD/RMS observations for one recording.

    Policies may read these fields. They must not mutate them. Evaluator truth
    is intentionally absent.
    """

    recording_id: str
    geometry_fingerprint: str
    geometry_canonical: str
    vad_backend: str
    sample_rate_hz: int
    audio: Any
    vad_centers_sec: Any
    vad_window_start_sec: Any
    vad_window_end_sec: Any
    vad_offset_samples: Any
    vad_speech_prob: Any
    frame_rms_dbfs: Any
    frame_voicing: Any
    buffers: tuple[BufferScope, ...]
    vad_observation_count: int
    vad_timestamp_sha256: str
    vad_probability_sha256: str
    bundle_id: str
    audio_path: str = ""

    def __post_init__(self) -> None:
        names = {item.name for item in fields(self)}
        leaked = [name for name in _FORBIDDEN_BUNDLE_FIELDS if name in names]
        if leaked:
            raise RuntimeError(f"feature bundle leaked annotation fields: {leaked}")
        object.__setattr__(self, "audio", _readonly_sequence(self.audio))
        object.__setattr__(self, "vad_centers_sec", _readonly_sequence(self.vad_centers_sec))
        object.__setattr__(
            self, "vad_window_start_sec", _readonly_sequence(self.vad_window_start_sec)
        )
        object.__setattr__(
            self, "vad_window_end_sec", _readonly_sequence(self.vad_window_end_sec)
        )
        object.__setattr__(
            self, "vad_offset_samples", _readonly_sequence(self.vad_offset_samples)
        )
        object.__setattr__(self, "vad_speech_prob", _readonly_sequence(self.vad_speech_prob))
        object.__setattr__(self, "frame_rms_dbfs", _readonly_sequence(self.frame_rms_dbfs))
        object.__setattr__(self, "frame_voicing", _readonly_sequence(self.frame_voicing))


def require_bundle_geometry(bundle: FeatureBundle, config: GeometryConfig) -> None:
    """Reject cross-geometry reuse. Same fingerprint only."""
    expected = geometry_fingerprint(config)
    if bundle.geometry_fingerprint != expected:
        raise RuntimeError(
            "feature bundle geometry mismatch: bundle "
            f"{bundle.geometry_fingerprint} != {config.name} {expected}"
        )


def buffer_bounds(bundle: FeatureBundle, buffer_id: str) -> tuple[float, float]:
    for buf in bundle.buffers:
        if buf.buffer_id == buffer_id:
            return float(buf.start_sec), float(buf.end_sec)
    raise RuntimeError(f"buffer {buffer_id!r} is not in the feature bundle")


def as_float_tuple(values: Any) -> tuple[float, ...]:
    return tuple(float(v) for v in values)
