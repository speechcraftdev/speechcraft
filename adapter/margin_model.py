"""Boring linear signed-margin inference. No trainer."""

from __future__ import annotations

import math
from dataclasses import dataclass

from adapter.config import feature_names_for_subset


@dataclass(frozen=True)
class MarginSpec:
    """Signed-margin regressor. Not part of the geometry fingerprint."""

    kind: str
    model_id: str
    target: str
    feature_subset: str
    feature_names: tuple[str, ...]
    feature_mean: tuple[float, ...]
    feature_scale: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float = 0.0
    cap_ms: float = 100.0

    def __post_init__(self) -> None:
        for name, values in (
            ("feature_mean", self.feature_mean),
            ("feature_scale", self.feature_scale),
            ("coefficients", self.coefficients),
        ):
            for index, value in enumerate(values):
                if not math.isfinite(float(value)):
                    raise RuntimeError(f"non-finite margin {name}[{index}]: {value}")
                if name == "feature_scale" and float(value) <= 0.0:
                    raise RuntimeError("margin feature_scale must be strictly positive")
        if not math.isfinite(float(self.intercept)):
            raise RuntimeError(f"non-finite margin intercept: {self.intercept}")
        if not math.isfinite(float(self.cap_ms)) or float(self.cap_ms) <= 0.0:
            raise RuntimeError(f"margin cap_ms must be positive, got {self.cap_ms}")
        if self.target != "signed_margin_ms":
            raise RuntimeError(f"margin.target must be 'signed_margin_ms', got {self.target!r}")


def require_margin_spec(spec: MarginSpec) -> None:
    if spec.kind != "linear_v1":
        raise RuntimeError(f"unknown margin kind: {spec.kind!r}")
    expected = feature_names_for_subset(spec.feature_subset)
    if spec.feature_names != expected:
        raise RuntimeError(
            f"margin feature_names {spec.feature_names} != subset {spec.feature_subset} {expected}"
        )
    n = len(expected)
    if len(spec.feature_mean) != n:
        raise RuntimeError(f"margin feature_mean length {len(spec.feature_mean)} != {n}")
    if len(spec.feature_scale) != n:
        raise RuntimeError(f"margin feature_scale length {len(spec.feature_scale)} != {n}")
    if len(spec.coefficients) != n:
        raise RuntimeError(f"margin coefficients length {len(spec.coefficients)} != {n}")


def margin_policy_payload(spec: MarginSpec) -> dict[str, object]:
    require_margin_spec(spec)
    return {
        "kind": spec.kind,
        "model_id": spec.model_id,
        "target": spec.target,
        "feature_subset": spec.feature_subset,
        "feature_names": list(spec.feature_names),
        "feature_mean": list(spec.feature_mean),
        "feature_scale": list(spec.feature_scale),
        "coefficients": list(spec.coefficients),
        "intercept": float(spec.intercept),
        "cap_ms": float(spec.cap_ms),
    }


def standardize(raw: tuple[float, ...] | list[float], spec: MarginSpec) -> tuple[float, ...]:
    require_margin_spec(spec)
    if len(raw) != len(spec.feature_names):
        raise RuntimeError(f"feature width {len(raw)} != {len(spec.feature_names)}")
    return tuple(
        (float(value) - spec.feature_mean[index]) / spec.feature_scale[index]
        for index, value in enumerate(raw)
    )


def predict_margin_ms(raw_features: tuple[float, ...] | list[float], spec: MarginSpec) -> float:
    z = standardize(raw_features, spec)
    return float(spec.intercept) + sum(
        float(coef) * float(value) for coef, value in zip(spec.coefficients, z)
    )


def preference_delta(margin_ms: float, *, score_scale: float, cap_ms: float) -> float:
    """Higher (more positive) predicted margin must increase packer preference."""
    if not math.isfinite(float(margin_ms)):
        raise RuntimeError(f"non-finite predicted margin: {margin_ms}")
    if not math.isfinite(float(score_scale)):
        raise RuntimeError(f"non-finite score_scale: {score_scale}")
    cap = float(cap_ms)
    if cap <= 0.0:
        raise RuntimeError(f"cap_ms must be positive, got {cap_ms}")
    clipped = min(cap, max(-cap, float(margin_ms)))
    return float(score_scale) * (clipped / cap)
