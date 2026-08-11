"""Adapter-side request / raw output types.

Structurally annotation-free: no phones, uncertainty, or RecordingReference.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adapter.config import GeometryConfig
from referee.types import BufferScope


@dataclass(frozen=True)
class SlicerRequest:
    """Everything the slicer needs for one recording — and nothing else.

    Allowed fields are audio location, sample rate, buffer scopes, and an
    explicit GeometryConfig. Annotations stay on RecordingReference only.
    """

    recording_id: str
    audio_path: Path
    sample_rate_hz: int
    buffers: tuple[BufferScope, ...]
    config: GeometryConfig


@dataclass(frozen=True)
class RawCutpoint:
    """Neutral raw cut before conversion to Phase-1 Cutpoint."""

    recording_id: str
    buffer_id: str
    time_sec: float


@dataclass(frozen=True)
class RawClip:
    """Neutral raw clip before conversion to Phase-1 Clip."""

    recording_id: str
    buffer_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class RawSlicerOutput:
    """Canonical-path (or stub) output before Phase-1 conversion."""

    cutpoints: tuple[RawCutpoint, ...]
    clips: tuple[RawClip, ...]
