"""Pure-Python logistic regression. No sklearn, no Buckeye training path."""

from __future__ import annotations

import math

from adapter.config import (
    LOGISTIC_BOUNDARY_FEATURE_NAMES,
    LogisticSpec,
)


def sigmoid(logit: float) -> float:
    """Numerically stable logistic. sigmoid(0) == 0.5."""
    if logit >= 0.0:
        exp_neg = math.exp(-logit)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(logit)
    return exp_pos / (1.0 + exp_pos)


def logit_from_features(
    features: tuple[float, ...] | list[float],
    weights: tuple[float, ...] | list[float],
    bias: float,
) -> float:
    if len(features) != len(weights):
        raise RuntimeError(
            f"logistic feature/weight length mismatch: {len(features)} != {len(weights)}"
        )
    return float(bias) + sum(float(weight) * float(value) for weight, value in zip(weights, features))


def predict_proba(features: tuple[float, ...] | list[float], spec: LogisticSpec) -> float:
    require_boundary_v1(spec)
    return sigmoid(logit_from_features(features, spec.weights, spec.bias))


def score_delta(prob: float, spec: LogisticSpec) -> float:
    """Centered so an untrained zero model is a no-op on detector scores."""
    return float(spec.score_scale) * (float(prob) - 0.5)


def require_boundary_v1(spec: LogisticSpec) -> None:
    if spec.kind != "boundary_v1":
        raise RuntimeError(f"unknown logistic kind: {spec.kind!r}")
    if spec.feature_names != LOGISTIC_BOUNDARY_FEATURE_NAMES:
        raise RuntimeError(
            f"logistic feature_names {spec.feature_names} != {LOGISTIC_BOUNDARY_FEATURE_NAMES}"
        )
    if len(spec.weights) != len(LOGISTIC_BOUNDARY_FEATURE_NAMES):
        raise RuntimeError(
            f"logistic weights length {len(spec.weights)} != {len(LOGISTIC_BOUNDARY_FEATURE_NAMES)}"
        )


def _require_binary_label(label: float, index: int) -> float:
    value = float(label)
    if value == 0.0 or value == 1.0:
        return value
    raise RuntimeError(f"logistic labels must be 0 or 1; got {label!r} at index {index}")


def fit_logistic(
    features: list[tuple[float, ...]] | tuple[tuple[float, ...], ...],
    labels: list[float] | tuple[float, ...],
    *,
    l2: float = 1e-4,
    learning_rate: float = 1.0,
    epochs: int = 500,
    score_scale: float = 1.0,
    model_id: str = "synthetic_fit",
) -> LogisticSpec:
    """Batch gradient descent on synthetic 0/1 labels. Not a Buckeye trainer."""
    rows = [tuple(float(value) for value in row) for row in features]
    if not rows:
        raise RuntimeError("logistic fit requires at least one example")
    if len(rows) != len(labels):
        raise RuntimeError(
            f"logistic fit feature/label length mismatch: {len(rows)} != {len(labels)}"
        )
    n_features = len(LOGISTIC_BOUNDARY_FEATURE_NAMES)
    for index, row in enumerate(rows):
        if len(row) != n_features:
            raise RuntimeError(
                f"logistic fit row {index} has {len(row)} features, expected {n_features}"
            )
    targets = [_require_binary_label(label, index) for index, label in enumerate(labels)]
    n = len(rows)
    weights = [0.0] * n_features
    bias = 0.0
    rate = float(learning_rate)
    penalty = float(l2)
    for _ in range(int(epochs)):
        grad_w = [0.0] * n_features
        grad_b = 0.0
        for row, target in zip(rows, targets):
            err = sigmoid(logit_from_features(row, weights, bias)) - target
            for i in range(n_features):
                grad_w[i] += err * row[i]
            grad_b += err
        for i in range(n_features):
            weights[i] -= rate * ((grad_w[i] / n) + penalty * weights[i])
        bias -= rate * (grad_b / n)
    return LogisticSpec(
        kind="boundary_v1",
        model_id=str(model_id),
        feature_names=LOGISTIC_BOUNDARY_FEATURE_NAMES,
        weights=tuple(weights),
        bias=float(bias),
        score_scale=float(score_scale),
    )
