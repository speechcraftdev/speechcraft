#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

from speaker_ts_eval.repaired_buckeye_benchmark import (  # noqa: E402
    RepairedMatrixFixture,
    run_repaired_detector_matrix,
)


BUCKEYE_ROOT = Path("/home/aaravthegreat/Datasets/buckeye")
COHORT_ROOT = BUCKEYE_ROOT / "eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort"
NORMALIZED_ROOT = BUCKEYE_ROOT / "normalized_reviewed_2026-07-18/corpus"
OUT_ROOT = BUCKEYE_ROOT / "eval_runs/2026-07-21_buckeye_vad_geometry_experiment"
BUNDLE = BUCKEYE_ROOT / "eval_runs/2026-07-21_buckeye_vad_geometry_experiment_review_bundle_no_audio.zip"

GEOMETRIES = {
    "A_current_overlap_8ms": {
        "backend": "silero_official_onnx",
        "window": 512,
        "hop": 256,
        "offsets": (0, 128),
        "description": "current overlapping recurrent streams, 8 ms combined spacing",
    },
    "D_four_sequential_8ms": {
        "backend": "silero_official_onnx",
        "window": 512,
        "hop": 512,
        "offsets": (0, 128, 256, 384),
        "description": "four independent sequential streams, 8 ms combined spacing",
    },
    "C_two_sequential_16ms": {
        "backend": "silero_official_onnx",
        "window": 512,
        "hop": 512,
        "offsets": (0, 256),
        "description": "two independent sequential streams, 16 ms combined spacing",
    },
    "B_standard_sequential_32ms": {
        "backend": "silero_official_onnx",
        "window": 512,
        "hop": 512,
        "offsets": (0,),
        "description": "one upstream-style sequential stream, 32 ms spacing",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return ""


def geometry_env(config: dict[str, Any]) -> dict[str, str]:
    return {
        "SPEAKER_TS_EVAL_VAD_BACKEND": str(config["backend"]),
        "SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES": str(config["window"]),
        "SPEAKER_TS_EVAL_VAD_HOP_SAMPLES": str(config["hop"]),
        "SPEAKER_TS_EVAL_VAD_OFFSETS": ",".join(str(offset) for offset in config["offsets"]),
    }


def load_speakers() -> list[dict[str, str]]:
    rows = read_csv(COHORT_ROOT / "speaker_cohort_summary.csv")
    return [row for row in rows if int(row["accepted_recording_count"]) > 0]


def build_fixtures(speakers: list[dict[str, str]]) -> list[RepairedMatrixFixture]:
    fixtures: list[RepairedMatrixFixture] = []
    for row in speakers:
        speaker_id = row["speaker_id"]
        fixtures.append(
            RepairedMatrixFixture(
                speaker_id=speaker_id,
                eval_run=COHORT_ROOT / "eval_runs" / speaker_id,
                normalized_speaker_dir=NORMALIZED_ROOT / speaker_id,
            )
        )
    return fixtures


def run_geometry_matrix(geometry: str, config: dict[str, Any], fixtures: list[RepairedMatrixFixture]) -> Path:
    geometry_root = OUT_ROOT / "matrices" / geometry
    if geometry_root.exists():
        shutil.rmtree(geometry_root)
    geometry_root.mkdir(parents=True)
    previous_env = {key: os.environ.get(key) for key in geometry_env(config)}
    os.environ.update(geometry_env(config))
    try:
        return run_repaired_detector_matrix(
            fixtures=fixtures,
            cluster_mapping_csv=COHORT_ROOT / "benchmark_cluster_mapping.csv",
            out_root=geometry_root,
            detector_names=["vad_percentile_rms"],
        )
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def collect_geometry_tables(geometry_dirs: dict[str, Path]) -> dict[str, list[dict[str, Any]]]:
    scorecards: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    unique: list[dict[str, Any]] = []
    final_edges: list[dict[str, Any]] = []
    runtime: list[dict[str, Any]] = []
    invariants: list[dict[str, Any]] = []
    run_index: list[dict[str, Any]] = []
    for geometry, matrix_dir in geometry_dirs.items():
        tables = matrix_dir / "tables"
        for row in read_csv(tables / "detector_scorecard_by_speaker.csv"):
            scorecards.append({"geometry": geometry, **row})
        for row in read_csv(tables / "coverage_by_recording_all.csv"):
            coverage.append({"geometry": geometry, **row})
        for row in read_csv(tables / "boundary_severity_unique_cutpoints_by_speaker.csv"):
            unique.append({"geometry": geometry, **row})
        for row in read_csv(tables / "boundary_severity_final_edge_occurrences_by_speaker.csv"):
            final_edges.append({"geometry": geometry, **row})
        for row in read_csv(tables / "runtime_by_detector_and_speaker.csv"):
            runtime.append({"geometry": geometry, **row})
        for row in read_csv(tables / "run_index.csv"):
            run_index.append({"geometry": geometry, **row})
            inv_path = Path(row["run_dir"]) / "tables" / "invariant_report.csv"
            for inv in read_csv(inv_path):
                invariants.append({"geometry": geometry, "speaker_id": row["speaker_id"], **inv})
    return {
        "scorecards": scorecards,
        "coverage": coverage,
        "unique": unique,
        "final_edges": final_edges,
        "runtime": runtime,
        "invariants": invariants,
        "run_index": run_index,
    }


def build_paired_speaker_deltas(scorecards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["geometry"], row["speaker_id"]): row for row in scorecards}
    rows: list[dict[str, Any]] = []
    for (geometry, speaker_id), row in sorted(by_key.items()):
        if geometry == "A_current_overlap_8ms":
            continue
        ref = by_key.get(("A_current_overlap_8ms", speaker_id))
        if ref is None:
            continue
        rows.append(
            {
                "speaker_id": speaker_id,
                "geometry": geometry,
                "coverage_delta_vs_A": round(numeric(row["conditional_target_phone_coverage"]) - numeric(ref["conditional_target_phone_coverage"]), 9),
                "inside_phone_rate_delta_vs_A": round(
                    numeric(first_present(row, ("inside_target_phone_rate_unique", "unique_inside_target_speech_phone_rate")))
                    - numeric(first_present(ref, ("inside_target_phone_rate_unique", "unique_inside_target_speech_phone_rate"))),
                    9,
                ),
                "gt50ms_rate_delta_vs_A": round(
                    numeric(first_present(row, ("gt_50ms_rate_unique", "unique_gt_50ms_rate")))
                    - numeric(first_present(ref, ("gt_50ms_rate_unique", "unique_gt_50ms_rate"))),
                    9,
                ),
                "selected_cutpoint_delta_vs_A": int(numeric(row["selected_cutpoint_count"]) - numeric(ref["selected_cutpoint_count"])),
                "selected_clip_delta_vs_A": int(numeric(row["selected_clip_count"]) - numeric(ref["selected_clip_count"])),
                "runtime_delta_sec_vs_A": round(numeric(first_present(row, ("runtime_sec", "wall_clock_sec"))) - numeric(first_present(ref, ("runtime_sec", "wall_clock_sec"))), 6),
            }
        )
    return rows


def build_paired_recording_deltas(coverage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(row["geometry"], row["speaker_id"], row["recording_id"]): row for row in coverage}
    rows: list[dict[str, Any]] = []
    for (geometry, speaker_id, recording_id), row in sorted(by_key.items()):
        if geometry == "A_current_overlap_8ms":
            continue
        ref = by_key.get(("A_current_overlap_8ms", speaker_id, recording_id))
        if ref is None:
            continue
        rows.append(
            {
                "speaker_id": speaker_id,
                "recording_id": recording_id,
                "geometry": geometry,
                "coverage_delta_vs_A": round(numeric(row["conditional_target_phone_coverage"]) - numeric(ref["conditional_target_phone_coverage"]), 9),
                "selected_clip_count_delta_vs_A": int(numeric(row["selected_clip_count"]) - numeric(ref["selected_clip_count"])),
            }
        )
    return rows


def summarize_deltas(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    import statistics

    out: list[dict[str, Any]] = []
    for geometry in sorted({row["geometry"] for row in rows}):
        values = [numeric(row[key]) for row in rows if row["geometry"] == geometry]
        if not values:
            continue
        wins = sum(value > 0 for value in values)
        losses = sum(value < 0 for value in values)
        ties = sum(value == 0 for value in values)
        out.append(
            {
                "geometry": geometry,
                "delta_key": key,
                "row_count": len(values),
                "median_delta": round(statistics.median(values), 9),
                "mean_delta": round(statistics.fmean(values), 9),
                "wins": wins,
                "losses": losses,
                "ties": ties,
            }
        )
    return out


def invariant_failure_count(rows: list[dict[str, Any]]) -> int:
    failure_invariants = {
        "cutpoints_outside_buffers",
        "clips_crossing_buffers",
        "clips_using_nemo_edges",
        "invented_boundaries",
        "unknown_phone_labels",
    }
    return sum(
        int(float(first_present(row, ("failed_count", "value"))))
        for row in rows
        if str(row.get("invariant", "")) in failure_invariants
    )


def build_boundary_review_manifest(geometry_dirs: dict[str, Path], limit_per_bucket: int = 50) -> list[dict[str, Any]]:
    import random

    rng = random.Random(0)
    rows: list[dict[str, Any]] = []
    a_index = {
        row["speaker_id"]: Path(row["run_dir"])
        for row in read_csv(geometry_dirs["A_current_overlap_8ms"] / "tables" / "run_index.csv")
    }
    d_index = {
        row["speaker_id"]: Path(row["run_dir"])
        for row in read_csv(geometry_dirs["D_four_sequential_8ms"] / "tables" / "run_index.csv")
    }
    for speaker_id in sorted(set(a_index) & set(d_index)):
        a_path = a_index[speaker_id] / "tables" / "selected_cutpoints.csv"
        d_path = d_index[speaker_id] / "tables" / "selected_cutpoints.csv"
        if not a_path.exists() or not d_path.exists():
            continue
        a_cuts = read_csv(a_path)
        d_cuts = read_csv(d_path)
        a_by_key = {(row["recording_id"], row["buffer_id"], round(numeric(row["time_sec"]), 3)): row for row in a_cuts}
        d_by_key = {(row["recording_id"], row["buffer_id"], round(numeric(row["time_sec"]), 3)): row for row in d_cuts}
        for label, keys, table in (
            ("A_only_selected_boundary", sorted(set(a_by_key) - set(d_by_key)), a_by_key),
            ("D_only_selected_boundary", sorted(set(d_by_key) - set(a_by_key)), d_by_key),
            ("unchanged_control_boundary", sorted(set(a_by_key) & set(d_by_key)), a_by_key),
        ):
            sample_keys = keys if len(keys) <= limit_per_bucket else rng.sample(keys, limit_per_bucket)
            for key in sorted(sample_keys):
                row = table[key]
                rows.append(
                    {
                        "review_bucket": label,
                        "speaker_id": speaker_id,
                        "recording_id": row["recording_id"],
                        "buffer_id": row["buffer_id"],
                        "geometry": "A_current_overlap_8ms" if label.startswith("A_") or label.startswith("unchanged") else "D_four_sequential_8ms",
                        "cutpoint_id": row["cutpoint_id"],
                        "timestamp_sec": row["time_sec"],
                        "suggested_window_start_sec": max(0.0, numeric(row["time_sec"]) - 1.0),
                        "suggested_window_end_sec": numeric(row["time_sec"]) + 1.0,
                    }
                )
    return rows


def package_bundle(geometry_dirs: dict[str, Path], tables_root: Path, test_log: Path) -> None:
    if BUNDLE.exists():
        BUNDLE.unlink()
    with zipfile.ZipFile(BUNDLE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tables_root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(OUT_ROOT))
        for geometry, matrix_dir in geometry_dirs.items():
            for relative in (
                "tables/detector_scorecard_by_speaker.csv",
                "tables/coverage_by_recording_all.csv",
                "tables/boundary_severity_unique_cutpoints_by_speaker.csv",
                "tables/boundary_severity_final_edge_occurrences_by_speaker.csv",
                "tables/runtime_by_detector_and_speaker.csv",
                "tables/run_index.csv",
                "metrics/provenance.json",
            ):
                path = matrix_dir / relative
                if path.exists():
                    zf.write(path, Path("matrices") / geometry / relative)
        for path in (
            Path(__file__),
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/buckeye_safecut_benchmark.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/repaired_buckeye_benchmark.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/slicer_daddy_test.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/intervals.py",
            SPEAKER_TS_EVAL_SRC / "speaker_ts_eval/io_utils.py",
            Path("/home/aaravthegreat/Projects/speaker_ts_eval/tests/test_buckeye_safecut_benchmark.py"),
            Path("/home/aaravthegreat/Projects/speaker_ts_eval/tests/test_slicer_daddy_test.py"),
            test_log,
        ):
            if path.exists():
                zf.write(path, Path("code_and_tests") / path.name)
        for path in (
            COHORT_ROOT / "cohort_manifest.csv",
            COHORT_ROOT / "speaker_cohort_summary.csv",
            COHORT_ROOT / "benchmark_cluster_mapping.csv",
        ):
            zf.write(path, Path("cohort") / path.name)


def main() -> None:
    start = time.perf_counter()
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True)
    test_log = OUT_ROOT / "test_log.txt"
    subprocess.run(
        ["uv", "run", "--with", "pytest", "python", "-m", "pytest", "-q"],
        cwd="/home/aaravthegreat/Projects/speaker_ts_eval",
        check=True,
        stdout=test_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    speakers = load_speakers()
    fixtures = build_fixtures(speakers)
    geometry_dirs: dict[str, Path] = {}
    for geometry, config in GEOMETRIES.items():
        matrix_dir = run_geometry_matrix(geometry, config, fixtures)
        geometry_dirs[geometry] = matrix_dir
    tables_root = OUT_ROOT / "tables"
    tables = collect_geometry_tables(geometry_dirs)
    for name, rows in tables.items():
        write_csv(tables_root / f"{name}.csv", rows, list(rows[0].keys()) if rows else ["geometry"])
    speaker_deltas = build_paired_speaker_deltas(tables["scorecards"])
    recording_deltas = build_paired_recording_deltas(tables["coverage"])
    write_csv(tables_root / "paired_speaker_deltas_vs_A.csv", speaker_deltas, list(speaker_deltas[0].keys()) if speaker_deltas else ["geometry"])
    write_csv(tables_root / "paired_recording_deltas_vs_A.csv", recording_deltas, list(recording_deltas[0].keys()) if recording_deltas else ["geometry"])
    delta_summary = summarize_deltas(recording_deltas, "coverage_delta_vs_A") + summarize_deltas(speaker_deltas, "gt50ms_rate_delta_vs_A")
    write_csv(tables_root / "paired_delta_summary.csv", delta_summary, list(delta_summary[0].keys()) if delta_summary else ["geometry"])
    review_manifest = build_boundary_review_manifest(geometry_dirs)
    write_csv(tables_root / "paired_A_vs_D_boundary_review_manifest.csv", review_manifest, list(review_manifest[0].keys()) if review_manifest else ["review_bucket"])
    invariant_failures = invariant_failure_count(tables["invariants"])
    provenance = {
        "experiment": "buckeye_vad_geometry_comparison",
        "detector": "vad_percentile_rms",
        "geometries": {name: {**config, "offsets": list(config["offsets"])} for name, config in GEOMETRIES.items()},
        "cohort_root": str(COHORT_ROOT),
        "speaker_count": len(speakers),
        "speakers": [row["speaker_id"] for row in speakers],
        "invariant_failures": invariant_failures,
        "runtime_sec": round(time.perf_counter() - start, 6),
        "bundle": str(BUNDLE),
    }
    (OUT_ROOT / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    package_bundle(geometry_dirs, tables_root, test_log)
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    main()
