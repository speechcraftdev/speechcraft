"""Phase 10C: signed-margin regression vs O0_4 score-control and pruning diagnostics."""

from __future__ import annotations

import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from adapter.buckeye_loader import LoadedRecording, load_recording
from adapter.buckeye_metrics import (
    format_geometry_table,
    geometry_comparison_table,
    metrics_from_evaluation,
    pool_metric_dicts,
)
from adapter.buckeye_validate import evaluate_loaded
from adapter.config import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_ID,
    FEATURE_SUBSET_ALL,
    O0_4,
    PHASE10B_N_SPLITS,
)
from adapter.diagnostics import geometry_fingerprint, hash_clip_intervals, hash_cutpoint_times
from adapter.logistic_cv import speaker_held_out_folds
from adapter.logistic_experiment import (
    extract_candidate_table,
    load_candidates_if_current,
    read_candidates_csv,
)
from adapter.logistic_labels import LABEL_SCHEMA_ID
from adapter.logistic_pack import pack_o0_4_baseline
from adapter.margin_cv import (
    fold_margin_counts,
    mean_coefficients,
    oof_margin_predictions,
    oof_usable_regression_metrics,
)
from adapter.margin_labels import (
    MARGIN_MODEL_ID,
    MARGIN_SCHEMA_ID,
    attach_margin_labels,
    count_margin_labels,
    count_margin_reasons,
)
from adapter.margin_model import margin_policy_payload
from adapter.margin_pack import (
    keep_frac_name,
    keep_top_score_fraction,
    pack_o0_4_keep,
    pack_o0_4_score_weight,
    pack_oof_margin,
    score_scale_name,
    score_weight_name,
)

# Frozen before looking at Buckeye. Do not add points after seeing results.
# keepXX is a candidate-pruning diagnostic, not the O0_4 coverage/safety frontier.
PHASE10C_KEEP_FRACS: tuple[float, ...] = (0.9, 0.8, 0.7, 0.6, 0.5)
# Detector-score contribution to packer weight. 0 is frozen O0_4 (duration-only).
PHASE10C_SCORE_WEIGHTS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 4.0)
PHASE10C_SCORE_SCALES: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)
PHASE10C_N_SPLITS = PHASE10B_N_SPLITS

SCORE_WEIGHT_NAMES: tuple[str, ...] = tuple(
    score_weight_name(weight) for weight in PHASE10C_SCORE_WEIGHTS
)
KEEP_NAMES: tuple[str, ...] = tuple(keep_frac_name(frac) for frac in PHASE10C_KEEP_FRACS)
MARGIN_NAMES: tuple[str, ...] = tuple(score_scale_name(scale) for scale in PHASE10C_SCORE_SCALES)
CONTENDER_NAMES: tuple[str, ...] = ("O0_4",) + SCORE_WEIGHT_NAMES + KEEP_NAMES + MARGIN_NAMES

FLOAT_COLUMNS = frozenset(
    FEATURE_NAMES
    + (
        "time_sec",
        "original_score",
        "interval_start_sec",
        "interval_end_sec",
    )
)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str] | None = None) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    names = fieldnames if fieldnames is not None else list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _margin_table_is_current(rows: list[dict[str, object]]) -> bool:
    if not rows:
        return False
    return all(
        str(row.get("schema_id") or "") == FEATURE_SCHEMA_ID
        and str(row.get("margin_schema_id") or "") == MARGIN_SCHEMA_ID
        and str(row.get("margin_reason") or "") != ""
        for row in rows
    )


def _parse_margin_row(row: dict[str, str]) -> dict[str, object]:
    parsed: dict[str, object] = dict(row)
    for name in FLOAT_COLUMNS:
        parsed[name] = float(row[name])
    raw = row.get("y_margin_ms", "")
    parsed["y_margin_ms"] = None if raw == "" else int(row["y_margin_ms"])
    parsed["margin_usable"] = int(row["margin_usable"])
    parsed["margin_reason"] = str(row["margin_reason"])
    parsed["margin_schema_id"] = str(row["margin_schema_id"])
    return parsed


def load_margin_candidates_if_current(path: Path) -> list[dict[str, object]] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [{str(k): str(v) for k, v in row.items() if k is not None} for row in csv.DictReader(handle)]
    if not _margin_table_is_current(rows):  # type: ignore[arg-type]
        return None
    return [_parse_margin_row(row) for row in rows]


def _load_feature_rows(
    subset: tuple[tuple[str, str], ...],
    output_dir: Path,
    *,
    candidates_csv: Path | None,
    workers: int,
    resume: bool,
) -> list[dict[str, object]]:
    cached = load_margin_candidates_if_current(output_dir / "candidates.csv")
    if resume and cached is not None:
        present = {(str(row["speaker_id"]), str(row["recording_id"])) for row in cached}
        if present == set(subset):
            return cached
    feature_rows: list[dict[str, object]] | None = None
    if candidates_csv is not None:
        feature_rows = read_candidates_csv(candidates_csv)
        present = {(str(row["speaker_id"]), str(row["recording_id"])) for row in feature_rows}
        if present != set(subset):
            raise RuntimeError(
                f"candidates csv cohort {sorted(present)} != requested subset {list(subset)}"
            )
    elif resume:
        feature_rows = load_candidates_if_current(output_dir / "candidates.csv")
        if feature_rows is not None:
            present = {(str(row["speaker_id"]), str(row["recording_id"])) for row in feature_rows}
            if present != set(subset):
                feature_rows = None
    if feature_rows is None:
        feature_rows = extract_candidate_table(
            subset, output_dir, workers=workers, resume=resume
        )
    references = {
        (speaker_id, recording_id): load_recording(speaker_id, recording_id).reference
        for speaker_id, recording_id in subset
    }
    rows = attach_margin_labels(feature_rows, references=references)
    fieldnames = list(feature_rows[0].keys()) if feature_rows else []
    extra = ["y_margin_ms", "margin_usable", "margin_reason", "margin_schema_id"]
    for name in extra:
        if name not in fieldnames:
            fieldnames.append(name)
    _write_csv(output_dir / "candidates.csv", rows, fieldnames=fieldnames)
    return rows


def train_oof_margin(
    rows: list[dict[str, object]],
    *,
    n_splits: int = PHASE10C_N_SPLITS,
) -> tuple[dict[str, float], dict[str, Any]]:
    speakers = sorted({str(row["speaker_id"]) for row in rows})
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    print(f"train OOF {MARGIN_MODEL_ID}", flush=True)
    predicted, specs = oof_margin_predictions(
        rows, subset=FEATURE_SUBSET_ALL, n_splits=len(folds)
    )
    models: dict[str, Any] = {
        "schema_id": FEATURE_SCHEMA_ID,
        "margin_schema_id": MARGIN_SCHEMA_ID,
        "label_schema_id": LABEL_SCHEMA_ID,
        "n_splits": len(folds),
        "folds": [
            {
                "fold": index,
                "train_speakers": list(train),
                "test_speakers": list(test),
            }
            for index, (train, test) in enumerate(folds)
        ],
        "models": {MARGIN_MODEL_ID: [margin_policy_payload(spec) for spec in specs]},
        "ranking": {MARGIN_MODEL_ID: oof_usable_regression_metrics(rows, predicted)},
        "fold_counts": {MARGIN_MODEL_ID: list(fold_margin_counts(rows, n_splits=len(folds)))},
        "coefficients": {MARGIN_MODEL_ID: mean_coefficients(specs)},
    }
    return predicted, models


def _recording_eval_row(
    *,
    speaker_id: str,
    recording_id: str,
    geometry: str,
    loaded: LoadedRecording,
    result: Any,
    candidate_count: int,
) -> dict[str, object]:
    score = evaluate_loaded(loaded, result)
    metrics = metrics_from_evaluation(score)
    return {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "geometry": geometry,
        **metrics.to_dict(),
        "geometry_fingerprint": geometry_fingerprint(O0_4),
        "candidate_count": candidate_count,
        "selected_cutpoint_sha256": hash_cutpoint_times(result.cutpoints),
        "selected_clip_sha256": hash_clip_intervals(result.clips),
    }


def evaluate_schedules(
    subset: tuple[tuple[str, str], ...],
    rows: list[dict[str, object]],
    predicted: dict[str, float],
) -> list[dict[str, object]]:
    by_rec: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_rec[(str(row["speaker_id"]), str(row["recording_id"]))].append(row)
    recording_rows: list[dict[str, object]] = []
    for speaker_id, recording_id in subset:
        loaded = load_recording(speaker_id, recording_id)
        rec_rows = by_rec[(speaker_id, recording_id)]
        if not rec_rows:
            raise RuntimeError(f"no candidates for {speaker_id}/{recording_id}")
        print(f"pack {speaker_id}/{recording_id} n={len(rec_rows)}", flush=True)
        baseline = pack_o0_4_baseline(rec_rows)
        recording_rows.append(
            _recording_eval_row(
                speaker_id=speaker_id,
                recording_id=recording_id,
                geometry="O0_4",
                loaded=loaded,
                result=baseline,
                candidate_count=len(rec_rows),
            )
        )
        for weight in PHASE10C_SCORE_WEIGHTS:
            name = score_weight_name(weight)
            result = pack_o0_4_score_weight(rec_rows, weight)
            recording_rows.append(
                _recording_eval_row(
                    speaker_id=speaker_id,
                    recording_id=recording_id,
                    geometry=name,
                    loaded=loaded,
                    result=result,
                    candidate_count=len(rec_rows),
                )
            )
        for frac in PHASE10C_KEEP_FRACS:
            name = keep_frac_name(frac)
            kept = keep_top_score_fraction(rec_rows, frac)
            result = pack_o0_4_keep(rec_rows, frac)
            recording_rows.append(
                _recording_eval_row(
                    speaker_id=speaker_id,
                    recording_id=recording_id,
                    geometry=name,
                    loaded=loaded,
                    result=result,
                    candidate_count=len(kept),
                )
            )
        for scale in PHASE10C_SCORE_SCALES:
            name = score_scale_name(scale)
            result = pack_oof_margin(
                rec_rows,
                predicted,
                score_scale=scale,
                model_id=name,
            )
            recording_rows.append(
                _recording_eval_row(
                    speaker_id=speaker_id,
                    recording_id=recording_id,
                    geometry=name,
                    loaded=loaded,
                    result=result,
                    candidate_count=len(rec_rows),
                )
            )
    return recording_rows


def schedule_diffs_vs_o0_4(recording_rows: list[dict[str, object]]) -> dict[str, dict[str, int]]:
    by_rec: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in recording_rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]))
        by_rec.setdefault(key, {})[str(row["geometry"])] = row
    names = [name for name in CONTENDER_NAMES if name != "O0_4"]
    diffs = {
        name: {"n_recordings": 0, "cut_hash_diff": 0, "clip_hash_diff": 0, "either_diff": 0}
        for name in names
    }
    for geos in by_rec.values():
        base = geos.get("O0_4")
        if base is None:
            continue
        base_cuts = str(base.get("selected_cutpoint_sha256") or "")
        base_clips = str(base.get("selected_clip_sha256") or "")
        for name in names:
            other = geos.get(name)
            if other is None:
                continue
            diffs[name]["n_recordings"] += 1
            cut_diff = str(other.get("selected_cutpoint_sha256") or "") != base_cuts
            clip_diff = str(other.get("selected_clip_sha256") or "") != base_clips
            if cut_diff:
                diffs[name]["cut_hash_diff"] += 1
            if clip_diff:
                diffs[name]["clip_hash_diff"] += 1
            if cut_diff or clip_diff:
                diffs[name]["either_diff"] += 1
    return diffs


def operating_points(pooled: dict[str, Any]) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    for name in CONTENDER_NAMES:
        metrics = pooled[name]
        payload = metrics.to_dict() if hasattr(metrics, "to_dict") else dict(metrics)
        if name == "O0_4":
            family = "o0_4_baseline"
        elif name.startswith("O0_4_sw"):
            family = "o0_4_score_control"
        elif name.startswith("O0_4_keep"):
            family = "o0_4_candidate_pruning"
        else:
            family = "signed_margin"
        points.append(
            {
                "name": name,
                "family": family,
                "speech_coverage": payload.get("speech_coverage"),
                "inside_phone_rate": payload.get("inside_phone_rate"),
                "depth_gt_20ms_rate": payload.get("depth_gt_20ms_rate"),
                "depth_gt_50ms_rate": payload.get("depth_gt_50ms_rate"),
                "depth_gt_100ms_rate": payload.get("depth_gt_100ms_rate"),
            }
        )
    return points


def run_phase10c(
    *,
    output_dir: Path,
    subset: tuple[tuple[str, str], ...],
    workers: int = 1,
    resume: bool = True,
    n_splits: int = PHASE10C_N_SPLITS,
    candidates_csv: Path | None = None,
    cohort_info: dict[str, object] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"schema_id={FEATURE_SCHEMA_ID}", flush=True)
    print(f"margin_schema_id={MARGIN_SCHEMA_ID}", flush=True)
    print(f"frozen O0_4 geo={geometry_fingerprint(O0_4)}", flush=True)
    rows = _load_feature_rows(
        subset,
        output_dir,
        candidates_csv=candidates_csv,
        workers=workers,
        resume=resume,
    )
    predicted, models = train_oof_margin(rows, n_splits=n_splits)
    oof_rows = []
    for row in rows:
        oof_rows.append(
            {
                "candidate_id": row["candidate_id"],
                "speaker_id": row["speaker_id"],
                "recording_id": row["recording_id"],
                "y_margin_ms": row["y_margin_ms"],
                "margin_usable": row["margin_usable"],
                "pred_margin_ms": predicted[str(row["candidate_id"])],
            }
        )
    _write_json(output_dir / "folds.json", {"folds": models["folds"], "n_splits": models["n_splits"]})
    _write_json(output_dir / "models.json", models)
    _write_csv(output_dir / "oof_predictions.csv", oof_rows)
    recording_rows = evaluate_schedules(subset, rows, predicted)
    _write_csv(output_dir / "recordings.csv", recording_rows)
    diffs = schedule_diffs_vs_o0_4(recording_rows)
    pooled: dict[str, Any] = {}
    for name in CONTENDER_NAMES:
        named = [row for row in recording_rows if str(row["geometry"]) == name]
        if len(named) != len(subset):
            raise RuntimeError(f"{name} row count {len(named)} != subset {len(subset)}")
        pooled[name] = pool_metric_dicts(named)
    table = geometry_comparison_table(pooled, CONTENDER_NAMES)
    table_text = format_geometry_table(table, CONTENDER_NAMES)
    summary = {
        "schema_id": FEATURE_SCHEMA_ID,
        "margin_schema_id": MARGIN_SCHEMA_ID,
        "geometry_fingerprint": geometry_fingerprint(O0_4),
        "n_recordings": len(subset),
        "n_speakers": len({speaker for speaker, _rec in subset}),
        "n_candidates": len(rows),
        "n_models": 1,
        "keep_fracs": list(PHASE10C_KEEP_FRACS),
        "score_weights": list(PHASE10C_SCORE_WEIGHTS),
        "score_scales": list(PHASE10C_SCORE_SCALES),
        "label_counts": count_margin_labels(rows),
        "label_reasons": count_margin_reasons(rows),
        "ranking": models.get("ranking", {}),
        "fold_counts": models.get("fold_counts", {}),
        "coefficients": models.get("coefficients", {}),
        "schedule_diffs_vs_o0_4": diffs,
        "pooled": {name: metrics.to_dict() for name, metrics in pooled.items()},
        "operating_points": operating_points(pooled),
        "geometry_table": table,
        "geometry_table_text": table_text,
        "elapsed_sec": time.monotonic() - started,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": cohort_info or {},
        "contenders": list(CONTENDER_NAMES),
        "silero_passes": "reuse cached 15-feature O0_4 table; one linear model; packer-only after extract",
    }
    _write_json(output_dir / "summary.json", summary)
    return summary
