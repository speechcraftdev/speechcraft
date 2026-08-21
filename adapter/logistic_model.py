"""Boring logistic inference: standardize, sigmoid, P(bad). No trainer."""

from __future__ import annotations

import math

from adapter.config import (
    LogisticSpec,
    feature_names_for_subset,
)


def sigmoid(logit: float) -> float:
    """Numerically stable logistic. sigmoid(0) == 0.5."""
    if logit >= 0.0:
        exp_neg = math.exp(-logit)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(logit)
    return exp_pos / (1.0 + exp_pos)


def require_p_bad_spec(spec: LogisticSpec) -> None:
    if spec.kind != "boundary_v1":
        raise RuntimeError(f"unknown logistic kind: {spec.kind!r}")
    if spec.target != "p_bad":
        raise RuntimeError(f"logistic.target must be 'p_bad', got {spec.target!r}")
    expected = feature_names_for_subset(spec.feature_subset)
    if spec.feature_names != expected:
        raise RuntimeError(
            f"logistic feature_names {spec.feature_names} != subset {spec.feature_subset} {expected}"
        )
    n = len(expected)
    if len(spec.feature_mean) != n:
        raise RuntimeError(f"logistic feature_mean length {len(spec.feature_mean)} != {n}")
    if len(spec.feature_scale) != n:
        raise RuntimeError(f"logistic feature_scale length {len(spec.feature_scale)} != {n}")
    if len(spec.coefficients) != n:
        raise RuntimeError(f"logistic coefficients length {len(spec.coefficients)} != {n}")
    if any(not math.isfinite(float(scale)) or float(scale) <= 0.0 for scale in spec.feature_scale):
        raise RuntimeError("logistic feature_scale must be strictly positive")
    if any(not math.isfinite(float(value)) for value in spec.feature_mean):
        raise RuntimeError("logistic feature_mean must be finite")
    if any(not math.isfinite(float(value)) for value in spec.coefficients):
        raise RuntimeError("logistic coefficients must be finite")
    if not math.isfinite(float(spec.intercept)):
        raise RuntimeError("logistic intercept must be finite")
    if not math.isfinite(float(spec.score_scale)):
        raise RuntimeError("logistic score_scale must be finite")


def standardize(raw: tuple[float, ...] | list[float], spec: LogisticSpec) -> tuple[float, ...]:
    require_p_bad_spec(spec)
    if len(raw) != len(spec.feature_mean):
        raise RuntimeError(
            f"logistic feature/mean length mismatch: {len(raw)} != {len(spec.feature_mean)}"
        )
    for name, value in zip(spec.feature_names, raw):
        if not math.isfinite(float(value)):
            raise RuntimeError(f"non-finite logistic feature {name}: {value}")
    return tuple(
        (float(value) - float(mean)) / float(scale)
        for value, mean, scale in zip(raw, spec.feature_mean, spec.feature_scale)
    )


def predict_p_bad(raw_features: tuple[float, ...] | list[float], spec: LogisticSpec) -> float:
    """P(bad boundary). Higher means a worse candidate."""
    standardized = standardize(raw_features, spec)
    logit = float(spec.intercept) + sum(
        float(coef) * float(value) for coef, value in zip(spec.coefficients, standardized)
    )
    return sigmoid(logit)


def preference_delta(p_bad: float, spec: LogisticSpec) -> float:
    """Higher p_bad must lower packer preference."""
    return float(spec.score_scale) * (0.5 - float(p_bad))
