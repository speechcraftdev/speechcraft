"""Wire historical tournament policies onto detector cutpoints / packer weights.

Cut-level helpers match code_snapshots/run_failure_driven_slicer_tournament.py.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from adapter.config import GeometryConfig
from adapter.feature_bundle import FeatureBundle
from adapter.logistic_policy import apply_logistic_cut_policy
from adapter.rms_evidence import FineRmsGrid
from adapter.tournament_policy import (
    BoundaryEvidence,
    apply_min_quiet_run,
    quiet_evidence_score,
)

_SCORE_DELTA_POLICIES = frozenset({"quiet_evidence", "logistic_boundary"})


def cut_quiet_run_ms(cut: Any) -> float:
    metadata = getattr(cut, "metadata", None) or {}
    return float(
        metadata.get("run_duration_ms")
        or max(0.0, (float(cut.interval_end_sec) - float(cut.interval_start_sec)) * 1000.0)
    )


def cut_quiet_before_ms(cut: Any) -> float:
    return max(0.0, (float(cut.time_sec) - float(cut.interval_start_sec)) * 1000.0)


def cut_quiet_after_ms(cut: Any) -> float:
    return max(0.0, (float(cut.interval_end_sec) - float(cut.time_sec)) * 1000.0)


def _replace_obj(obj: Any, **changes: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return replace(obj, **changes)
    payload = dict(vars(obj))
    payload.update(changes)
    return SimpleNamespace(**payload)


def attach_quiet_metadata(cut: Any) -> Any:
    metadata = dict(getattr(cut, "metadata", None) or {})
    metadata.update(
        {
            "quiet_run_ms": round(cut_quiet_run_ms(cut), 6),
            "quiet_before_ms": round(cut_quiet_before_ms(cut), 6),
            "quiet_after_ms": round(cut_quiet_after_ms(cut), 6),
        }
    )
    return _replace_obj(cut, metadata=metadata)


def boundary_evidence_from_cut(cut: Any) -> BoundaryEvidence:
    metadata = getattr(cut, "metadata", None) or {}
    rms = getattr(cut, "rms_min_dbfs", None)
    return BoundaryEvidence(
        boundary_id=str(cut.cutpoint_id),
        time_sec=float(cut.time_sec),
        quiet_run_ms=cut_quiet_run_ms(cut),
        quiet_before_ms=cut_quiet_before_ms(cut),
        quiet_after_ms=cut_quiet_after_ms(cut),
        rms_dbfs=float(rms if rms is not None else -60.0),
        local_prominence_db=float(metadata.get("score_max", 0.0) or 0.0),
        base_score=float(cut.score),
    )


def apply_cut_policy(
    cuts: list[Any],
    config: GeometryConfig,
    bundle: FeatureBundle | None = None,
    fine_grid: FineRmsGrid | None = None,
) -> list[Any]:
    """Apply min-quiet gate and/or rescoring. Baseline is a no-op."""
    if config.logistic is not None and config.scoring != "logistic_boundary":
        raise RuntimeError(
            f"{config.name} has a logistic spec but scoring={config.scoring!r}"
        )
    annotated = [attach_quiet_metadata(cut) for cut in cuts]
    if config.min_quiet_run_ms is not None:
        min_ms = float(config.min_quiet_run_ms)
        annotated = [
            cut
            for cut in annotated
            if apply_min_quiet_run(boundary_evidence_from_cut(cut), min_ms).accepted
        ]
    if config.scoring == "quiet_evidence":
        rescored: list[Any] = []
        for cut in annotated:
            decision = quiet_evidence_score(boundary_evidence_from_cut(cut))
            metadata = dict(getattr(cut, "metadata", None) or {})
            metadata["policy_reasons"] = ";".join(decision.reasons)
            metadata["original_score"] = cut.score
            rescored.append(_replace_obj(cut, score=round(decision.score, 6), metadata=metadata))
        annotated = rescored
    elif config.scoring == "logistic_boundary":
        if bundle is None:
            raise RuntimeError("logistic_boundary scoring requires a feature bundle")
        annotated = apply_logistic_cut_policy(annotated, config, bundle, fine_grid)
    elif config.scoring not in {None, ""}:
        raise RuntimeError(f"unknown scoring policy: {config.scoring!r}")
    return annotated


def apply_candidate_weight_policy(
    candidates: list[Any],
    cuts: list[Any],
    config: GeometryConfig,
) -> list[Any]:
    """Historical quiet_run_score packer: add boundary-score delta to clip weight."""
    if config.scoring in _SCORE_DELTA_POLICIES:
        by_id = {str(cut.cutpoint_id): cut for cut in cuts}
        weighted: list[Any] = []
        for candidate in candidates:
            start = by_id[str(candidate.start_cutpoint_id)]
            end = by_id[str(candidate.end_cutpoint_id)]
            start_meta = getattr(start, "metadata", None) or {}
            end_meta = getattr(end, "metadata", None) or {}
            original_boundary_score = float(start_meta.get("original_score", start.score)) + float(
                end_meta.get("original_score", end.score)
            )
            adjusted_boundary_score = float(start.score) + float(end.score)
            weight_delta = adjusted_boundary_score - original_boundary_score
            weighted.append(_replace_obj(candidate, weight=round(float(candidate.weight) + weight_delta, 6)))
        return weighted
    spec = config.rms_policy
    if spec is None or spec.kind == "current":
        return candidates
    by_id = {str(cut.cutpoint_id): cut for cut in cuts}
    weighted = []
    for candidate in candidates:
        start = by_id[str(candidate.start_cutpoint_id)]
        end = by_id[str(candidate.end_cutpoint_id)]
        start_delta = float((getattr(start, "metadata", None) or {}).get("score_delta", 0.0))
        end_delta = float((getattr(end, "metadata", None) or {}).get("score_delta", 0.0))
        if start_delta == 0.0 and end_delta == 0.0:
            weighted.append(candidate)
            continue
        weighted.append(
            _replace_obj(
                candidate,
                weight=round(float(candidate.weight) + start_delta + end_delta, 6),
            )
        )
    return weighted
