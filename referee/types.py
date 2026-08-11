"""Neutral benchmark data types for the Phase 1 referee.

These structures are the only contract between a future slicer adapter and
the evaluator. Annotations on RecordingReference are evaluator-only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BufferScope:
    """Immutable allowed scope identified by buffer_id within a recording."""

    buffer_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class Cutpoint:
    recording_id: str
    buffer_id: str
    time_sec: float


@dataclass(frozen=True)
class Clip:
    recording_id: str
    buffer_id: str
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class PhoneInterval:
    """Trusted target-speech phone interval (evaluator-only annotation)."""

    recording_id: str
    buffer_id: str
    start_sec: float
    end_sec: float
    phone: str


@dataclass(frozen=True)
class UncertaintyInterval:
    """Uncertainty mask (evaluator-only annotation)."""

    recording_id: str
    buffer_id: str
    start_sec: float
    end_sec: float


@dataclass(frozen=True)
class RecordingReference:
    recording_id: str
    buffers: tuple[BufferScope, ...]
    phones: tuple[PhoneInterval, ...]
    uncertainty_intervals: tuple[UncertaintyInterval, ...]


@dataclass(frozen=True)
class SlicerResult:
    cutpoints: tuple[Cutpoint, ...]
    clips: tuple[Clip, ...]


@dataclass(frozen=True)
class UniqueCutSafetyMetrics:
    """Primary safety population: unique detector-selected internal cutpoints."""

    unique_cutpoint_count: int
    inside_phone_count: int
    inside_phone_rate: float | None
    depth_gt_20ms_count: int
    depth_gt_50ms_count: int
    depth_gt_100ms_count: int
    depth_gt_20ms_rate: float | None
    depth_gt_50ms_rate: float | None
    depth_gt_100ms_rate: float | None


@dataclass(frozen=True)
class EdgeSafetyMetrics:
    """Secondary population: final emitted clip edge occurrences (not deduped)."""

    edge_occurrence_count: int
    inside_phone_count: int
    inside_phone_rate: float | None
    depth_gt_20ms_count: int
    depth_gt_50ms_count: int
    depth_gt_100ms_count: int
    depth_gt_20ms_rate: float | None
    depth_gt_50ms_rate: float | None
    depth_gt_100ms_rate: float | None


@dataclass(frozen=True)
class EvaluationResult:
    unique_cut_safety: UniqueCutSafetyMetrics
    final_edge_safety: EdgeSafetyMetrics
    eligible_target_speech_sec: float
    retained_target_speech_sec: float
    speech_coverage: float | None
    emitted_audio_sec: float
    clip_count: int
