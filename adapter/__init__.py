"""Phase 2: A/D execution adapters over the Phase-1 neutral contract."""

from adapter.config import CURRENT_A, PROPER_D, GeometryConfig
from adapter.convert import to_slicer_result
from adapter.run import run_geometry, run_slicer
from adapter.types import RawClip, RawCutpoint, RawSlicerOutput, SlicerRequest

__all__ = [
    "CURRENT_A",
    "PROPER_D",
    "GeometryConfig",
    "RawClip",
    "RawCutpoint",
    "RawSlicerOutput",
    "SlicerRequest",
    "run_geometry",
    "run_slicer",
    "to_slicer_result",
]
