"""Apply P(bad) logistic scoring to detector cutpoints.

Does not add or drop candidates. Untrained zero coefficients leave scores unchanged.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from adapter.config import GeometryConfig, LogisticSpec
from adapter.feature_bundle import FeatureBundle, require_bundle_geometry
from adapter.logistic_features import LOGISTIC_FINE_RMS_SPEC, features_from_cut_for_config
from adapter.logistic_model import predict_p_bad, preference_delta, require_p_bad_spec
from adapter.rms_evidence import FineRmsGrid, build_fine_rms_grid


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
    require_p_bad_spec(config.logistic)
    return config.logistic


def apply_logistic_cut_policy(
    cuts: list[Any],
    config: GeometryConfig,
    bundle: FeatureBundle,
    fine_grid: FineRmsGrid | None = None,
) -> list[Any]:
    spec = require_logistic_spec(config)
    require_bundle_geometry(bundle, config)
    grid = fine_grid if fine_grid is not None else build_fine_rms_grid(bundle, LOGISTIC_FINE_RMS_SPEC)
    updated: list[Any] = []
    for cut in cuts:
        features = features_from_cut_for_config(cut, bundle, config, fine_grid=grid)
        p_bad = predict_p_bad(features, spec)
        delta = preference_delta(p_bad, spec)
        metadata = dict(getattr(cut, "metadata", None) or {})
        metadata["original_score"] = float(cut.score)
        metadata["logistic_p_bad"] = round(p_bad, 6)
        metadata["score_delta"] = round(delta, 6)
        metadata["logistic_model_id"] = spec.model_id
        metadata["logistic_target"] = "p_bad"
        metadata["policy_reasons"] = "logistic_boundary"
        updated.append(
            _replace_obj(
                cut,
                score=round(float(cut.score) + delta, 6),
                metadata=metadata,
            )
        )
    return updated
