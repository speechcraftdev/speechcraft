"""Faithful copies of the historical failure-driven tournament policy helpers.

Source: code_snapshots/failure_driven_tournament_policy.py
Only the two Phase-6 contender functions and their types are included.
Do not "improve" these formulas.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BoundaryRole(str, Enum):
    START = "start"
    END = "end"
    INTERNAL = "internal"


@dataclass(frozen=True)
class BoundaryEvidence:
    boundary_id: str
    time_sec: float
    role: BoundaryRole = BoundaryRole.INTERNAL
    source: str = "A"
    quiet_run_ms: float = 0.0
    quiet_before_ms: float = 0.0
    quiet_after_ms: float = 0.0
    rms_dbfs: float = -60.0
    local_prominence_db: float = 0.0
    a_speech_prob: float | None = None
    d_speech_prob: float | None = None
    distance_to_scope_edge_ms: float = float("inf")
    trusted_source_edge: bool = False
    base_score: float = 1.0


@dataclass(frozen=True)
class BoundaryDecision:
    boundary: BoundaryEvidence
    accepted: bool
    score: float
    reasons: tuple[str, ...]


def apply_min_quiet_run(boundary: BoundaryEvidence, min_ms: float) -> BoundaryDecision:
    accepted = boundary.quiet_run_ms + 1e-9 >= min_ms
    return BoundaryDecision(
        boundary=boundary,
        accepted=accepted,
        score=boundary.base_score,
        reasons=() if accepted else (f"quiet_run_lt_{min_ms:g}ms",),
    )


def quiet_evidence_score(boundary: BoundaryEvidence) -> BoundaryDecision:
    score = (
        boundary.base_score
        + min(boundary.quiet_run_ms, 250.0) / 250.0
        + min(boundary.quiet_before_ms, 120.0) / 240.0
        + min(boundary.quiet_after_ms, 120.0) / 240.0
        + max(0.0, -boundary.rms_dbfs) / 100.0
        + max(0.0, boundary.local_prominence_db) / 20.0
    )
    return BoundaryDecision(boundary=boundary, accepted=True, score=score, reasons=("quiet_evidence_score",))
