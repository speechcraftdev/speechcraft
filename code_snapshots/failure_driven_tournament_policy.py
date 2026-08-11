from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable


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
    distance_to_scope_edge_ms: float = math.inf
    trusted_source_edge: bool = False
    base_score: float = 1.0


@dataclass(frozen=True)
class BoundaryDecision:
    boundary: BoundaryEvidence
    accepted: bool
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class CandidateClipEvidence:
    clip_id: str
    start: BoundaryEvidence
    end: BoundaryEvidence
    duration_sec: float
    retained_speech_sec: float
    preferred_duration_penalty: float = 0.0


@dataclass(frozen=True)
class PolicyParameters:
    d_veto_speech_prob: float = 0.50
    d_soft_penalty_weight: float = 0.50
    a_only_rms_extra_db: float = 4.0
    consensus_score: float = 3.0
    d_only_score: float = 2.0
    a_only_score: float = 1.0
    high_conf_quiet_run_ms: float = 80.0
    catastrophic_quiet_run_ms: float = 80.0
    catastrophic_one_sided_ms: float = 48.0
    catastrophic_penalty: float = 1000.0
    scope_edge_zone_ms: float = 200.0
    scope_edge_soft_penalty: float = 0.5
    stronger_rms_threshold_dbfs: float = -50.0


def dangerous_side_quiet_ms(boundary: BoundaryEvidence) -> float:
    if boundary.role == BoundaryRole.START:
        return boundary.quiet_before_ms
    if boundary.role == BoundaryRole.END:
        return boundary.quiet_after_ms
    return min(boundary.quiet_before_ms, boundary.quiet_after_ms)


def apply_min_quiet_run(boundary: BoundaryEvidence, min_ms: float) -> BoundaryDecision:
    accepted = boundary.quiet_run_ms + 1e-9 >= min_ms
    return BoundaryDecision(
        boundary=boundary,
        accepted=accepted,
        score=boundary.base_score,
        reasons=() if accepted else (f"quiet_run_lt_{min_ms:g}ms",),
    )


def apply_one_sided_gate(boundary: BoundaryEvidence, min_ms: float) -> BoundaryDecision:
    observed = dangerous_side_quiet_ms(boundary)
    accepted = observed + 1e-9 >= min_ms
    return BoundaryDecision(
        boundary=boundary,
        accepted=accepted,
        score=boundary.base_score,
        reasons=() if accepted else (f"dangerous_side_quiet_lt_{min_ms:g}ms",),
    )


def apply_one_sided_soft_penalty(boundary: BoundaryEvidence, min_ms: float) -> BoundaryDecision:
    missing = max(0.0, min_ms - dangerous_side_quiet_ms(boundary))
    penalty = missing / max(min_ms, 1e-9)
    return BoundaryDecision(
        boundary=boundary,
        accepted=True,
        score=boundary.base_score - penalty,
        reasons=() if missing == 0 else ("one_sided_soft_penalty",),
    )


def apply_hard_d_veto(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.d_speech_prob is not None and boundary.d_speech_prob >= params.d_veto_speech_prob:
        return BoundaryDecision(boundary=boundary, accepted=False, score=boundary.base_score, reasons=("D_speech_veto",))
    return BoundaryDecision(boundary=boundary, accepted=True, score=boundary.base_score, reasons=())


def apply_soft_d_penalty(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.d_speech_prob is None:
        return BoundaryDecision(boundary=boundary, accepted=True, score=boundary.base_score, reasons=("D_missing",))
    penalty = max(0.0, boundary.d_speech_prob - params.d_veto_speech_prob) * params.d_soft_penalty_weight
    return BoundaryDecision(
        boundary=boundary,
        accepted=True,
        score=boundary.base_score - penalty,
        reasons=() if penalty == 0 else ("D_soft_penalty",),
    )


def apply_a_only_stronger_rms(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.source != "A_only":
        return BoundaryDecision(boundary=boundary, accepted=True, score=boundary.base_score, reasons=())
    accepted = boundary.rms_dbfs <= params.stronger_rms_threshold_dbfs
    return BoundaryDecision(
        boundary=boundary,
        accepted=accepted,
        score=boundary.base_score,
        reasons=() if accepted else ("A_only_rms_not_strong_enough",),
    )


def consensus_rank_score(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.source == "A_and_D":
        score = params.consensus_score
    elif boundary.source == "D_only":
        score = params.d_only_score
    elif boundary.source == "A_only":
        score = params.a_only_score
    else:
        score = boundary.base_score
    return BoundaryDecision(boundary=boundary, accepted=True, score=score, reasons=(f"source_rank_{boundary.source}",))


def apply_scope_edge_soft_penalty(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.trusted_source_edge or boundary.distance_to_scope_edge_ms >= params.scope_edge_zone_ms:
        return BoundaryDecision(boundary=boundary, accepted=True, score=boundary.base_score, reasons=())
    proximity = 1.0 - (boundary.distance_to_scope_edge_ms / max(params.scope_edge_zone_ms, 1e-9))
    return BoundaryDecision(
        boundary=boundary,
        accepted=True,
        score=boundary.base_score - (params.scope_edge_soft_penalty * proximity),
        reasons=("scope_edge_soft_penalty",),
    )


def apply_scope_edge_hard_zone(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    if boundary.trusted_source_edge or boundary.distance_to_scope_edge_ms >= params.scope_edge_zone_ms:
        return BoundaryDecision(boundary=boundary, accepted=True, score=boundary.base_score, reasons=())
    return BoundaryDecision(boundary=boundary, accepted=False, score=boundary.base_score, reasons=("scope_edge_hard_zone",))


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


def catastrophic_boundary_penalty(boundary: BoundaryEvidence, params: PolicyParameters = PolicyParameters()) -> BoundaryDecision:
    catastrophic = (
        boundary.quiet_run_ms < params.catastrophic_quiet_run_ms
        or dangerous_side_quiet_ms(boundary) < params.catastrophic_one_sided_ms
    )
    return BoundaryDecision(
        boundary=boundary,
        accepted=True,
        score=boundary.base_score - (params.catastrophic_penalty if catastrophic else 0.0),
        reasons=("catastrophic_boundary_penalty",) if catastrophic else (),
    )


def safety_first_clip_key(clip: CandidateClipEvidence) -> tuple[float, float, float, str]:
    start_risk = _boundary_risk(clip.start)
    end_risk = _boundary_risk(clip.end)
    return (
        max(start_risk, end_risk),
        start_risk + end_risk,
        -clip.retained_speech_sec,
        clip.clip_id,
    )


def duration_first_clip_key(clip: CandidateClipEvidence) -> tuple[float, float, str]:
    return (
        clip.preferred_duration_penalty,
        -clip.retained_speech_sec,
        clip.clip_id,
    )


def _boundary_risk(boundary: BoundaryEvidence) -> float:
    quiet_risk = max(0.0, 80.0 - boundary.quiet_run_ms) / 80.0
    side_risk = max(0.0, 48.0 - dangerous_side_quiet_ms(boundary)) / 48.0
    rms_risk = max(0.0, boundary.rms_dbfs + 50.0) / 50.0
    return quiet_risk + side_risk + rms_risk


def select_d_primary_with_a_fallback(boundaries: Iterable[BoundaryEvidence], can_form_legal_clip: bool) -> list[BoundaryEvidence]:
    rows = list(boundaries)
    d_rows = [row for row in rows if row.source in {"D_only", "A_and_D"}]
    if d_rows and can_form_legal_clip:
        return d_rows
    return rows


def select_two_stage_boundaries(boundaries: Iterable[BoundaryEvidence], params: PolicyParameters = PolicyParameters()) -> tuple[list[BoundaryEvidence], list[BoundaryEvidence]]:
    high: list[BoundaryEvidence] = []
    fallback: list[BoundaryEvidence] = []
    for row in boundaries:
        if row.quiet_run_ms >= params.high_conf_quiet_run_ms and dangerous_side_quiet_ms(row) >= params.catastrophic_one_sided_ms:
            high.append(row)
        else:
            fallback.append(row)
    return high, fallback


REQUIRED_SCORECARD_COLUMNS = (
    "conditional_target_phone_coverage",
    "inside_target_phone_rate_unique",
    "gt_50ms_rate_unique",
)


def strict_numeric(row: dict[str, Any], key: str) -> float:
    if key not in row or row[key] in {None, ""}:
        raise ValueError(f"missing required metric: {key}")
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"non-finite metric {key}: {row[key]}")
    return value


def validate_scorecard_row(row: dict[str, Any]) -> None:
    for key in REQUIRED_SCORECARD_COLUMNS:
        value = strict_numeric(row, key)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"rate metric {key} out of range: {value}")
    for key in ("emitted_clip_count", "selected_cutpoint_count"):
        if key in row and row[key] not in {None, ""} and strict_numeric(row, key) < 0:
            raise ValueError(f"negative count metric {key}: {row[key]}")


def annotate_known_failures(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    required = {"failure_27_bad_end", "failure_49_bad_start", "failure_93_scope_contamination"}
    out = list(rows)
    present = {str(row.get("failure_id")) for row in out}
    missing = sorted(required - present)
    if missing:
        raise ValueError(f"missing annotated failure rows: {missing}")
    return out
