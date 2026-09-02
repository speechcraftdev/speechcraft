"""Speaker-held-out folds, train-fold standardization, sklearn linear margin fit."""

from __future__ import annotations

import math
from collections.abc import Sequence

from adapter.config import FEATURE_SCHEMA_ID, FEATURE_SUBSET_ALL, feature_names_for_subset
from adapter.logistic_cv import speaker_held_out_folds, train_fold_mean_scale
from adapter.margin_labels import MARGIN_CAP_MS, MARGIN_SCHEMA_ID
from adapter.margin_model import MarginSpec, predict_margin_ms


def fit_linear_spec(
    features: Sequence[tuple[float, ...]],
    labels: Sequence[float],
    *,
    subset: str,
    model_id: str,
) -> MarginSpec:
    """sklearn LinearRegression on train-fold-standardized features."""
    names = feature_names_for_subset(subset)
    if len(features) != len(labels):
        raise RuntimeError("feature/label length mismatch")
    if not features:
        raise RuntimeError("empty training fold")
    if any(len(row) != len(names) for row in features):
        raise RuntimeError("training features do not match subset width")
    if any(not math.isfinite(float(value)) for row in features for value in row):
        raise RuntimeError("non-finite training feature")
    if any(not math.isfinite(float(value)) for value in labels):
        raise RuntimeError("non-finite training margin")
    mean, scale = train_fold_mean_scale(features)
    standardized = [
        tuple((float(value) - mean[i]) / scale[i] for i, value in enumerate(row))
        for row in features
    ]
    try:
        import numpy as np
        from sklearn.linear_model import LinearRegression
    except ImportError as exc:
        raise RuntimeError("Phase 10C training requires sklearn and numpy") from exc
    model = LinearRegression()
    model.fit(np.asarray(standardized, dtype=float), np.asarray(list(labels), dtype=float))
    coefficients = tuple(float(value) for value in model.coef_)
    intercept = float(model.intercept_)
    spec = MarginSpec(
        kind="linear_v1",
        model_id=str(model_id),
        target="signed_margin_ms",
        feature_subset=subset,
        feature_names=names,
        feature_mean=mean,
        feature_scale=scale,
        coefficients=coefficients,
        intercept=intercept,
        cap_ms=float(MARGIN_CAP_MS),
    )
    sklearn_y = [float(value) for value in model.predict(np.asarray(standardized, dtype=float))]
    ours = [predict_margin_ms(row, spec) for row in features]
    if any(abs(left - right) > 1e-6 for left, right in zip(sklearn_y, ours)):
        raise RuntimeError("sklearn LinearRegression diverged from frozen linear inference")
    return spec


def oof_margin_predictions(
    rows: Sequence[dict[str, object]],
    *,
    subset: str = FEATURE_SUBSET_ALL,
    n_splits: int = 5,
) -> tuple[dict[str, float], tuple[MarginSpec, ...]]:
    """One OOF predicted margin per candidate. Fit uses usable rows only."""
    names = feature_names_for_subset(subset)
    speakers = [str(row["speaker_id"]) for row in rows]
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    predicted: dict[str, float] = {}
    specs: list[MarginSpec] = []
    for fold_index, (train_speakers, test_speakers) in enumerate(folds):
        train_set = set(train_speakers)
        test_set = set(test_speakers)
        train_rows = [row for row in rows if str(row["speaker_id"]) in train_set]
        test_rows = [row for row in rows if str(row["speaker_id"]) in test_set]
        if not train_rows or not test_rows:
            raise RuntimeError(f"fold {fold_index} has empty train or test rows")
        train_usable = [row for row in train_rows if int(row["margin_usable"]) == 1]
        if len(train_usable) < 2:
            raise RuntimeError(f"fold {fold_index} has too few usable margin training rows")
        train_x = [tuple(float(row[name]) for name in names) for row in train_usable]
        train_y = [float(row["y_margin_ms"]) for row in train_usable]
        spec = fit_linear_spec(
            train_x,
            train_y,
            subset=subset,
            model_id=f"{FEATURE_SCHEMA_ID}:{MARGIN_SCHEMA_ID}:{subset}:fold{fold_index}",
        )
        specs.append(spec)
        for row in test_rows:
            candidate_id = str(row["candidate_id"])
            if candidate_id in predicted:
                raise RuntimeError(f"duplicate OOF prediction for {candidate_id}")
            features = tuple(float(row[name]) for name in names)
            predicted[candidate_id] = predict_margin_ms(features, spec)
    if len(predicted) != len(rows):
        raise RuntimeError(
            f"OOF coverage {len(predicted)} != candidate rows {len(rows)}"
        )
    return predicted, tuple(specs)


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mx = sum(xs) / float(len(xs))
    my = sum(ys) / float(len(ys))
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx <= 0.0 or dy <= 0.0:
        return None
    return num / (dx * dy)


def _ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg
        i = j
    return ranks


def oof_usable_regression_metrics(
    rows: Sequence[dict[str, object]],
    predicted: dict[str, float],
) -> dict[str, float | int | None]:
    """Fit quality on held-out usable candidates only."""
    y_true: list[float] = []
    y_pred: list[float] = []
    for row in rows:
        if int(row["margin_usable"]) != 1:
            continue
        y_true.append(float(row["y_margin_ms"]))
        y_pred.append(float(predicted[str(row["candidate_id"])]))
    n = len(y_true)
    payload: dict[str, float | int | None] = {
        "n_usable": n,
        "mae": None,
        "rmse": None,
        "pearson": None,
        "spearman": None,
    }
    if n == 0:
        return payload
    mae = sum(abs(a - b) for a, b in zip(y_true, y_pred)) / float(n)
    rmse = math.sqrt(sum((a - b) ** 2 for a, b in zip(y_true, y_pred)) / float(n))
    payload["mae"] = mae
    payload["rmse"] = rmse
    payload["pearson"] = _pearson(y_true, y_pred)
    payload["spearman"] = _pearson(_ranks(y_true), _ranks(y_pred))
    return payload


def fold_margin_counts(
    rows: Sequence[dict[str, object]],
    *,
    n_splits: int = 5,
) -> tuple[dict[str, int], ...]:
    speakers = [str(row["speaker_id"]) for row in rows]
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    out: list[dict[str, int]] = []
    for fold_index, (train_speakers, test_speakers) in enumerate(folds):
        train_set = set(train_speakers)
        test_set = set(test_speakers)

        def _count(subset: set[str]) -> dict[str, int]:
            usable = 0
            unknown = 0
            for row in rows:
                if str(row["speaker_id"]) not in subset:
                    continue
                if int(row["margin_usable"]) == 1:
                    usable += 1
                else:
                    unknown += 1
            return {"usable": usable, "unknown": unknown}

        train_counts = _count(train_set)
        test_counts = _count(test_set)
        out.append(
            {
                "fold": fold_index,
                "train_usable": train_counts["usable"],
                "train_unknown": train_counts["unknown"],
                "test_usable": test_counts["usable"],
                "test_unknown": test_counts["unknown"],
            }
        )
    return tuple(out)


def mean_coefficients(specs: Sequence[MarginSpec]) -> list[dict[str, object]]:
    if not specs:
        return []
    names = list(specs[0].feature_names)
    rows: list[dict[str, object]] = []
    for index, name in enumerate(names):
        values = [float(spec.coefficients[index]) for spec in specs]
        mean = sum(values) / float(len(values))
        variance = sum((value - mean) ** 2 for value in values) / float(len(values))
        rows.append(
            {
                "feature": name,
                "mean": mean,
                "std": math.sqrt(variance),
                "folds": values,
            }
        )
    intercepts = [float(spec.intercept) for spec in specs]
    intercept_mean = sum(intercepts) / float(len(intercepts))
    intercept_var = sum((value - intercept_mean) ** 2 for value in intercepts) / float(
        len(intercepts)
    )
    rows.append(
        {
            "feature": "intercept",
            "mean": intercept_mean,
            "std": math.sqrt(intercept_var),
            "folds": intercepts,
        }
    )
    return rows
