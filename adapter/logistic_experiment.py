"""Phase 10B: one O0_4 candidate table, speaker-held-out OOF logistic, frozen packer."""

from __future__ import annotations

import csv
import json
import multiprocessing
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
    O0_4,
    PHASE10B_FEATURE_SETS,
    PHASE10B_N_SPLITS,
    PHASE10B_TARGETS,
    logistic_policy_payload,
)
from adapter.diagnostics import geometry_fingerprint, hash_clip_intervals, hash_cutpoint_times
from adapter.logistic_cv import (
    fold_state_counts,
    mean_coefficients,
    model_name,
    oof_known_ranking_metrics,
    oof_predictions,
    speaker_held_out_folds,
)
from adapter.logistic_extract import CANDIDATE_COLUMNS, extract_recording_candidates
from adapter.logistic_labels import LABEL_SCHEMA_ID, PHASE10B_STATE_TARGETS, count_label_states
from adapter.logistic_pack import pack_o0_4_baseline, pack_oof_model

CONTENDER_NAMES: tuple[str, ...] = ("O0_4",) + tuple(
    model_name(subset, target)
    for subset in PHASE10B_FEATURE_SETS
    for target in PHASE10B_TARGETS
)
FLOAT_COLUMNS = frozenset(
    FEATURE_NAMES
    + (
        "time_sec",
        "original_score",
        "interval_start_sec",
        "interval_end_sec",
    )
)
INT_COLUMNS = frozenset(("y_inside", "y_gt20ms", "y_gt50ms"))
STATE_COLUMNS = frozenset(("state_inside", "state_gt20ms", "state_gt50ms"))


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


def candidate_table_is_current(rows: list[dict[str, object]]) -> bool:
    if not rows:
        return False
    return all(
        str(row.get("schema_id") or "") == FEATURE_SCHEMA_ID
        and str(row.get("label_schema_id") or "") == LABEL_SCHEMA_ID
        for row in rows
    )


def _parse_candidate_row(row: dict[str, str]) -> dict[str, object]:
    parsed: dict[str, object] = dict(row)
    for name in FLOAT_COLUMNS:
        parsed[name] = float(row[name])
    for name in INT_COLUMNS:
        raw = row.get(name, "")
        parsed[name] = None if raw == "" else int(raw)
    for name in STATE_COLUMNS:
        parsed[name] = str(row[name])
    return parsed


def read_candidates_csv(path: Path) -> list[dict[str, object]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [{str(k): str(v) for k, v in row.items() if k is not None} for row in csv.DictReader(handle)]
    if not candidate_table_is_current(rows):  # type: ignore[arg-type]
        raise RuntimeError(
            f"candidate table is stale: need schema_id={FEATURE_SCHEMA_ID} "
            f"label_schema_id={LABEL_SCHEMA_ID}"
        )
    return [_parse_candidate_row(row) for row in rows]


def load_candidates_if_current(path: Path) -> list[dict[str, object]] | None:
    """Return parsed rows, or None if the cache is missing/stale. Never KeyError on old CSVs."""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [{str(k): str(v) for k, v in row.items() if k is not None} for row in csv.DictReader(handle)]
    if not candidate_table_is_current(rows):  # type: ignore[arg-type]
        return None
    return [_parse_candidate_row(row) for row in rows]


def _extract_shard_job(payload: dict[str, object]) -> dict[str, object]:
    speaker_id = str(payload["speaker_id"])
    recording_id = str(payload["recording_id"])
    shard_path = Path(str(payload["shard_path"]))
    if shard_path.is_file() and shard_path.stat().st_size > 0 and payload.get("resume"):
        rows = json.loads(shard_path.read_text(encoding="utf-8"))
        if rows and candidate_table_is_current(rows):
            return {
                "speaker_id": speaker_id,
                "recording_id": recording_id,
                "n_candidates": len(rows),
                "resumed": True,
            }
    loaded = load_recording(speaker_id, recording_id)
    rows = extract_recording_candidates(loaded)
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = shard_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    tmp.replace(shard_path)
    return {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "n_candidates": len(rows),
        "resumed": False,
    }


def extract_candidate_table(
    subset: tuple[tuple[str, str], ...],
    output_dir: Path,
    *,
    workers: int = 1,
    resume: bool = True,
) -> list[dict[str, object]]:
    shards_dir = output_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    candidates_path = output_dir / "candidates.csv"
    if resume:
        cached = load_candidates_if_current(candidates_path)
        if cached is not None:
            present = {(str(row["speaker_id"]), str(row["recording_id"])) for row in cached}
            if present == set(subset):
                ordered = []
                by_rec: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
                for row in cached:
                    by_rec[(str(row["speaker_id"]), str(row["recording_id"]))].append(row)
                for key in subset:
                    ordered.extend(by_rec[key])
                return ordered
    jobs = [
        {
            "speaker_id": speaker_id,
            "recording_id": recording_id,
            "shard_path": str(shards_dir / f"{recording_id}.json"),
            "resume": resume,
        }
        for speaker_id, recording_id in subset
    ]
    if workers < 1:
        raise RuntimeError(f"workers must be >= 1, got {workers}")
    if workers == 1:
        for index, job in enumerate(jobs, start=1):
            print(
                f"[{index}/{len(jobs)}] extract {job['speaker_id']}/{job['recording_id']}",
                flush=True,
            )
            _extract_shard_job(job)
    else:
        print(f"parallel extract workers={workers} recordings={len(jobs)}", flush=True)
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            done = 0
            for item in pool.imap_unordered(_extract_shard_job, jobs):
                done += 1
                print(
                    f"  extracted {item['speaker_id']}/{item['recording_id']} "
                    f"n={item['n_candidates']} resumed={item['resumed']} "
                    f"done={done}/{len(jobs)}",
                    flush=True,
                )
    rows: list[dict[str, object]] = []
    for speaker_id, recording_id in subset:
        shard = json.loads((shards_dir / f"{recording_id}.json").read_text(encoding="utf-8"))
        if not shard:
            raise RuntimeError(f"{recording_id} shard is empty")
        for row in shard:
            if (str(row["speaker_id"]), str(row["recording_id"])) != (speaker_id, recording_id):
                raise RuntimeError(f"shard identity mismatch for {recording_id}")
            if str(row["schema_id"]) != FEATURE_SCHEMA_ID:
                raise RuntimeError(f"{recording_id} schema drifted from {FEATURE_SCHEMA_ID}")
            if str(row.get("label_schema_id") or "") != LABEL_SCHEMA_ID:
                raise RuntimeError(
                    f"{recording_id} label schema drifted from {LABEL_SCHEMA_ID}"
                )
        rows.extend(shard)
    _write_csv(candidates_path, rows, fieldnames=list(CANDIDATE_COLUMNS))
    return rows


def train_oof_models(
    rows: list[dict[str, object]],
    *,
    n_splits: int = PHASE10B_N_SPLITS,
) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    speakers = sorted({str(row["speaker_id"]) for row in rows})
    folds = speaker_held_out_folds(speakers, n_splits=n_splits)
    oof: dict[str, dict[str, float]] = {}
    models: dict[str, Any] = {
        "schema_id": FEATURE_SCHEMA_ID,
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
        "models": {},
    }
    for subset in PHASE10B_FEATURE_SETS:
        for target in PHASE10B_TARGETS:
            name = model_name(subset, target)
            print(f"train OOF {name}", flush=True)
            predicted, specs = oof_predictions(
                rows, subset=subset, target=target, n_splits=len(folds)
            )
            oof[name] = predicted
            models["models"][name] = [logistic_policy_payload(spec) for spec in specs]
            models.setdefault("ranking", {})[name] = oof_known_ranking_metrics(
                rows, predicted, target=target
            )
            models.setdefault("fold_counts", {})[name] = list(
                fold_state_counts(rows, target=target, n_splits=len(folds))
            )
            models.setdefault("coefficients", {})[name] = mean_coefficients(specs)
    return oof, models


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


def evaluate_oof_schedules(
    subset: tuple[tuple[str, str], ...],
    rows: list[dict[str, object]],
    oof: dict[str, dict[str, float]],
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
        for name, predicted in oof.items():
            result = pack_oof_model(rec_rows, predicted, model_id=name)
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


def run_phase10b(
    *,
    output_dir: Path,
    subset: tuple[tuple[str, str], ...],
    workers: int = 1,
    resume: bool = True,
    n_splits: int = PHASE10B_N_SPLITS,
    cohort_info: dict[str, object] | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"schema_id={FEATURE_SCHEMA_ID}", flush=True)
    print(f"label_schema_id={LABEL_SCHEMA_ID}", flush=True)
    print(f"frozen O0_4 geo={geometry_fingerprint(O0_4)}", flush=True)
    rows = extract_candidate_table(subset, output_dir, workers=workers, resume=resume)
    oof, models = train_oof_models(rows, n_splits=n_splits)
    oof_rows = []
    for row in rows:
        item: dict[str, object] = {
            "candidate_id": row["candidate_id"],
            "speaker_id": row["speaker_id"],
            "recording_id": row["recording_id"],
            "y_inside": row["y_inside"],
            "y_gt20ms": row["y_gt20ms"],
            "y_gt50ms": row["y_gt50ms"],
            "state_inside": row["state_inside"],
            "state_gt20ms": row["state_gt20ms"],
            "state_gt50ms": row["state_gt50ms"],
        }
        for name, predicted in oof.items():
            item[f"p_bad_{name}"] = predicted[str(row["candidate_id"])]
        oof_rows.append(item)
    _write_json(output_dir / "folds.json", {"folds": models["folds"], "n_splits": models["n_splits"]})
    _write_json(output_dir / "models.json", models)
    _write_csv(output_dir / "oof_predictions.csv", oof_rows)
    recording_rows = evaluate_oof_schedules(subset, rows, oof)
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
    label_states = count_label_states(rows)
    known_rates = {}
    for target in PHASE10B_STATE_TARGETS:
        known = label_states[target]["bad"] + label_states[target]["safe"]
        known_rates[target] = None if known == 0 else label_states[target]["bad"] / float(known)
    summary = {
        "schema_id": FEATURE_SCHEMA_ID,
        "label_schema_id": LABEL_SCHEMA_ID,
        "geometry_fingerprint": geometry_fingerprint(O0_4),
        "n_recordings": len(subset),
        "n_speakers": len({speaker for speaker, _rec in subset}),
        "n_candidates": len(rows),
        "n_models": len(CONTENDER_NAMES) - 1,
        "label_states": label_states,
        "known_positive_rate": known_rates,
        "ranking": models.get("ranking", {}),
        "fold_counts": models.get("fold_counts", {}),
        "coefficients": models.get("coefficients", {}),
        "schedule_diffs_vs_o0_4": diffs,
        "pooled": {name: metrics.to_dict() for name, metrics in pooled.items()},
        "geometry_table": table,
        "geometry_table_text": table_text,
        "elapsed_sec": time.monotonic() - started,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "cohort": cohort_info or {},
        "contenders": list(CONTENDER_NAMES),
        "silero_passes": "one O0_4 detector extract per recording; 9 models reuse the cached table",
    }
    _write_json(output_dir / "summary.json", summary)
    return summary
