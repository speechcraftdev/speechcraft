"""Speaker-held-out folds, train-fold standardization, sklearn logistic fit."""

from __future__ import annotations

import math
from collections.abc import Sequence

from adapter.config import (
    FEATURE_SCHEMA_ID,
    LogisticSpec,
    feature_names_for_subset,
)
from adapter.logistic_labels import TRAIN_STATES, y_from_state
from adapter.logistic_model import predict_p_bad


def speaker_held_out_folds(
    speaker_ids: Sequence[str],
    *,
    n_splits: int = 5,
) -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Disjoint speaker test folds. Training speakers never appear in the test fold."""
    unique = tuple(sorted(set(str(speaker) for speaker in speaker_ids)))
    if len(unique) < 2:
        raise RuntimeError("speaker-held-out CV needs at least 2 speakers")
    splits = min(int(n_splits), len(unique))
    if splits < 2:
        raise RuntimeError("speaker-held-out CV needs at least 2 folds")
    folds: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for fold_index in range(splits):
        test = tuple(unique[index] for index in range(fold_index, len(unique), splits))
        train = tuple(speaker for speaker in unique if speaker not in set(test))
        if not train or not test:
            raise RuntimeError(f"empty train or test speakers in fold {fold_index}")
        overlap = set(train) & set(test)
        if overlap:
            raise RuntimeError(f"speaker leak in fold {fold_index}: {sorted(overlap)}")
        folds.append((train, test))
    assigned = [speaker for _train, test in folds for speaker in test]
    if sorted(assigned) != sorted(unique):
        raise RuntimeError("speaker-held-out folds do not cover every speaker exactly once")
    return tuple(folds)


def train_fold_mean_scale(
    rows: Sequence[tuple[float, ...]],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Population mean/std from the training fold only. Zero-variance scale becomes 1."""
    if not rows:
        raise RuntimeError("cannot standardize an empty training fold")
    n_features = len(rows[0])
    if any(len(row) != n_features for row in rows):
        raise RuntimeError("training fold has ragged feature rows")
    n = len(rows)
    means: list[float] = []
    scales: list[float] = []
    for index in range(n_features):
        column = [float(row[index]) for row in rows]
        if any(not math.isfinite(value) for value in column):
            raise RuntimeError(f"non-finite training feature at column {index}")
        mean = sum(column) / float(n)
        variance = sum((value - mean) ** 2 for value in column) / float(n)
        scale = math.sqrt(variance)
        if scale <= 0.0:
            scale = 1.0
        means.append(mean)
        scales.append(scale)
    return tuple(means), tuple(scales)


def _logit(probability: float) -> float:
    clipped = min(1.0 - 1e-12, max(1e-12, float(probability)))
    return math.log(clipped / (1.0 - clipped))


def fit_logistic_spec(
    features: Sequence[tuple[float, ...]],
    labels: Sequence[int],
    *,
    subset: str,
    model_id: str,
) -> LogisticSpec:
    """sklearn logistic on train-fold-standardized features. y=1 is the bad class."""
    names = feature_names_for_subset(subset)
    if len(features) != len(labels):
        raise RuntimeError("feature/label length mismatch")
    if not features:
        raise RuntimeError("empty training fold")
    if any(len(row) != len(names) for row in features):
        raise RuntimeError("training features do not match subset width")
    if any(label not in (0, 1) for label in labels):
        raise RuntimeError("logistic labels must be 0 or 1")
    mean, scale = train_fold_mean_scale(features)
    standardized = [
        tuple((float(value) - mean[i]) / scale[i] for i, value in enumerate(row))
        for row in features
    ]
    positive = sum(int(label) for label in labels)
    if positive == 0 or positive == len(labels):
        rate = positive / float(len(labels))
        return LogisticSpec(
            kind="boundary_v1",
            model_id=str(model_id),
            target="p_bad",
            feature_subset=subset,
            feature_names=names,
            feature_mean=mean,
            feature_scale=scale,
            coefficients=(0.0,) * len(names),
            intercept=_logit(rate),
            score_scale=1.0,
        )
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError as exc:
        raise RuntimeError("Phase 10B training requires sklearn and numpy") from exc
    model = LogisticRegression(
        C=1.0,
        solver="lbfgs",
        max_iter=2000,
        random_state=0,
    )
    model.fit(np.asarray(standardized, dtype=float), np.asarray(list(labels), dtype=int))
    coefficients = tuple(float(value) for value in model.coef_[0])
    intercept = float(model.intercept_[0])
    spec = LogisticSpec(
        kind="boundary_v1",
        model_id=str(model_id),
        target="p_bad",
        feature_subset=subset,
        feature_names=names,
        feature_mean=mean,
        feature_scale=scale,
        coefficients=coefficients,
        intercept=intercept,
        score_scale=1.0,
    )
    # Sanity: our sigmoid path must match sklearn on the training fold.
    sklearn_p = [float(p) for p in model.predict_proba(np.asarray(standardized, dtype=float))[:, 1]]
    ours = [predict_p_bad(row, spec) for row in features]
    if any(abs(left - right) > 1e-6 for left, right in zip(sklearn_p, ours)):
        raise RuntimeError("sklearn predict_proba diverged from frozen sigmoid inference")
    return spec


def oof_predictions(
    rows: Sequence[dict[str, object]],
    *,
    subset: str,
    target: str,
    n_splits: int = 5,
) -> tuple[dict[str, float], tuple[LogisticSpec, ...]]:
    """One OOF p_bad per candidate. Fit uses known bad/safe rows only."""
    names = feature_names_for_subset(subset)
    state_key = f"state_{target}"
    speakers = [str(row["speaker_id"]) for row in rows]
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    predicted: dict[str, float] = {}
    specs: list[LogisticSpec] = []
    for fold_index, (train_speakers, test_speakers) in enumerate(folds):
        train_set = set(train_speakers)
        test_set = set(test_speakers)
        train_rows = [row for row in rows if str(row["speaker_id"]) in train_set]
        test_rows = [row for row in rows if str(row["speaker_id"]) in test_set]
        if not train_rows or not test_rows:
            raise RuntimeError(f"fold {fold_index} has empty train or test rows")
        train_known = [
            row for row in train_rows if str(row[state_key]) in TRAIN_STATES
        ]
        if not train_known:
            raise RuntimeError(f"fold {fold_index} has no known {target} training rows")
        train_x = [tuple(float(row[name]) for name in names) for row in train_known]
        train_y = [int(y_from_state(str(row[state_key]))) for row in train_known]
        if any(label not in (0, 1) for label in train_y):
            raise RuntimeError(f"fold {fold_index} produced a non-binary known label")
        spec = fit_logistic_spec(
            train_x,
            train_y,
            subset=subset,
            model_id=f"{FEATURE_SCHEMA_ID}:{subset}:{target}:fold{fold_index}",
        )
        specs.append(spec)
        for row in test_rows:
            candidate_id = str(row["candidate_id"])
            if candidate_id in predicted:
                raise RuntimeError(f"duplicate OOF prediction for {candidate_id}")
            features = tuple(float(row[name]) for name in names)
            predicted[candidate_id] = predict_p_bad(features, spec)
    if len(predicted) != len(rows):
        raise RuntimeError(
            f"OOF coverage {len(predicted)} != candidate rows {len(rows)}"
        )
    return predicted, tuple(specs)


def model_name(subset: str, target: str) -> str:
    return f"O0_4_LR_{subset}_{target}"


def parse_model_name(name: str) -> tuple[str, str]:
    prefix = "O0_4_LR_"
    if not name.startswith(prefix):
        raise RuntimeError(f"not a Phase 10B model name: {name!r}")
    rest = name[len(prefix) :]
    for target in ("gt50ms", "gt20ms", "inside"):
        suffix = f"_{target}"
        if rest.endswith(suffix):
            return rest[: -len(suffix)], target
    raise RuntimeError(f"cannot parse Phase 10B model name: {name!r}")


def fold_state_counts(
    rows: Sequence[dict[str, object]],
    *,
    target: str,
    n_splits: int = 5,
) -> tuple[dict[str, int], ...]:
    state_key = f"state_{target}"
    speakers = [str(row["speaker_id"]) for row in rows]
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    out: list[dict[str, int]] = []
    for fold_index, (train_speakers, test_speakers) in enumerate(folds):
        train_set = set(train_speakers)
        test_set = set(test_speakers)

        def _count(subset: set[str]) -> dict[str, int]:
            counts = {"bad": 0, "safe": 0, "unknown": 0}
            for row in rows:
                if str(row["speaker_id"]) not in subset:
                    continue
                state = str(row[state_key])
                if state not in counts:
                    raise RuntimeError(f"invalid state {state!r}")
                counts[state] += 1
            return counts

        train_counts = _count(train_set)
        test_counts = _count(test_set)
        out.append(
            {
                "fold": fold_index,
                "train_bad": train_counts["bad"],
                "train_safe": train_counts["safe"],
                "train_unknown": train_counts["unknown"],
                "test_bad": test_counts["bad"],
                "test_safe": test_counts["safe"],
                "test_unknown": test_counts["unknown"],
            }
        )
    return tuple(out)


def oof_known_ranking_metrics(
    rows: Sequence[dict[str, object]],
    predicted: dict[str, float],
    *,
    target: str,
) -> dict[str, float | int | None]:
    """ROC/PR-AUC on held-out known (bad/safe) candidates only."""
    state_key = f"state_{target}"
    labels: list[int] = []
    scores: list[float] = []
    for row in rows:
        state = str(row[state_key])
        if state not in TRAIN_STATES:
            continue
        label = y_from_state(state)
        if label is None:
            raise RuntimeError("known row produced a None label")
        labels.append(int(label))
        scores.append(float(predicted[str(row["candidate_id"])]))
    n_known = len(labels)
    n_bad = sum(labels)
    payload: dict[str, float | int | None] = {
        "n_known": n_known,
        "n_bad": n_bad,
        "n_safe": n_known - n_bad,
        "roc_auc": None,
        "pr_auc": None,
    }
    if n_known == 0 or n_bad == 0 or n_bad == n_known:
        return payload
    try:
        from sklearn.metrics import average_precision_score, roc_auc_score
    except ImportError as exc:
        raise RuntimeError("Phase 10B ranking metrics require sklearn") from exc
    payload["roc_auc"] = float(roc_auc_score(labels, scores))
    payload["pr_auc"] = float(average_precision_score(labels, scores))
    return payload


def mean_coefficients(specs: Sequence[LogisticSpec]) -> list[dict[str, object]]:
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
    intercept_var = sum((value - intercept_mean) ** 2 for value in intercepts) / float(len(intercepts))
    rows.append(
        {
            "feature": "intercept",
            "mean": intercept_mean,
            "std": math.sqrt(intercept_var),
            "folds": intercepts,
        }
    )
    return rows
