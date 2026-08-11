"""Phase 2 slicer adapter: one path, A/D differ only by GeometryConfig.

Public result is always Phase-1 SlicerResult (cutpoints + clips).
Annotations never cross this boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields

from adapter.config import CURRENT_A, PROPER_D, GeometryConfig
from adapter.convert import to_slicer_result
from adapter.types import RawSlicerOutput, SlicerRequest
from referee.types import SlicerResult

# Executor protocol: request in, raw cuts/clips out. No annotation types.
SlicerExecutor = Callable[[SlicerRequest], RawSlicerOutput]

# Fields allowed on the adapter request — structural annotation ignorance.
_ALLOWED_REQUEST_FIELDS = frozenset(
    {"recording_id", "audio_path", "sample_rate_hz", "buffers", "config"}
)
_FORBIDDEN_REQUEST_FIELDS = frozenset(
    {
        "phones",
        "phone",
        "uncertainty_intervals",
        "uncertainty",
        "reference",
        "recording_reference",
        "words",
        "annotations",
    }
)


def _default_executor(request: SlicerRequest) -> RawSlicerOutput:
    from adapter.canonical_executor import execute_canonical

    return execute_canonical(request)


def run_slicer(
    request: SlicerRequest,
    *,
    executor: SlicerExecutor | None = None,
) -> SlicerResult:
    """Invoke the slicer for one recording and return Phase-1 SlicerResult.

    A and D both call this function; pass CURRENT_A or PROPER_D as request.config.
    Optional ``executor`` is for tests / stubs. Default uses the canonical
    vad_percentile_rms path (fresh state per call; no shared acoustic cache).
    """
    assert set(f.name for f in fields(SlicerRequest)) == _ALLOWED_REQUEST_FIELDS
    assert _FORBIDDEN_REQUEST_FIELDS.isdisjoint(_ALLOWED_REQUEST_FIELDS)

    if not isinstance(request.config, GeometryConfig):
        raise TypeError("request.config must be a GeometryConfig")

    active = executor if executor is not None else _default_executor
    raw = active(request)
    return to_slicer_result(raw)


def run_geometry(
    request_without_config: SlicerRequest,
    config: GeometryConfig,
    *,
    executor: SlicerExecutor | None = None,
) -> SlicerResult:
    """Same adapter path with an explicit geometry overlay (A or D)."""
    # Rebuild so callers cannot accidentally share a mutated request object.
    overlaid = SlicerRequest(
        recording_id=request_without_config.recording_id,
        audio_path=request_without_config.audio_path,
        sample_rate_hz=request_without_config.sample_rate_hz,
        buffers=request_without_config.buffers,
        config=config,
    )
    return run_slicer(overlaid, executor=executor)


__all__ = [
    "CURRENT_A",
    "PROPER_D",
    "GeometryConfig",
    "SlicerExecutor",
    "SlicerRequest",
    "run_geometry",
    "run_slicer",
]
