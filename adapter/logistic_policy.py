"""Apply logistic boundary scoring to detector cutpoints.

Does not add or drop candidates. Untrained zero weights leave scores unchanged.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from adapter.config import GeometryConfig, LogisticSpec
from adapter.logistic_features import features_from_cut
from adapter.logistic_model import predict_proba, require_boundary_v1, score_delta


def _replace_obj(obj: Any, **changes: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return replace(obj, **changes)
    payload = dict(vars(obj))
    payload.update(changes)
    return SimpleNamespace(**payload)


def require_logistic_spec(config: GeometryConfig) -> LogisticSpec:
    if config.scoring != "logistic_boundary":
        raise RuntimeError(
            f"{config.name} scoring={config.scoring!r} is not logistic_boundary"
        )
    if config.logistic is None:
        raise RuntimeError(
            f"{config.name} has logistic_boundary scoring but no logistic spec; "
            "refusing silent skip"
        )
    require_boundary_v1(config.logistic)
    return config.logistic


def apply_logistic_cut_policy(cuts: list[Any], config: GeometryConfig) -> list[Any]:
    spec = require_logistic_spec(config)
    updated: list[Any] = []
    for cut in cuts:
        features = features_from_cut(cut)
        prob = predict_proba(features, spec)
        delta = score_delta(prob, spec)
        metadata = dict(getattr(cut, "metadata", None) or {})
        metadata["original_score"] = float(cut.score)
        metadata["logistic_prob"] = round(prob, 6)
        metadata["score_delta"] = round(delta, 6)
        metadata["logistic_model_id"] = spec.model_id
        metadata["policy_reasons"] = "logistic_boundary"
        updated.append(
            _replace_obj(
                cut,
                score=round(float(cut.score) + delta, 6),
                metadata=metadata,
            )
        )
    return updated
