#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

from speaker_ts_eval.repaired_buckeye_benchmark import (  # noqa: E402
    build_interviewer_mask,
    build_phone_annotations,
    build_raw_nemo_target_buffers,
    classify_boundary,
    load_nemo_cluster_mapping,
    load_recording_eligibility,
    load_uncertainty_masks_by_recording,
    require_parser_artifacts,
)

ROOT = Path(
    "/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1"
)
MATRIX_ROOT = ROOT / "full" / (
    "2026-07-17_000000_buckeye_s01_s02_s03_s04_s05_s06_s07_s08_s09_s10_s11_s12_s13_"
    "s14_s15_s16_s17_s18_s19_s20_s21_s22_s24_repaired_matrix"
)
COHORT_ROOT = ROOT / "cohort"
PER_RUN_ROOT = MATRIX_ROOT / "per_run"
OUTPUT_DIR = Path(
    "/home/aaravthegreat/Projects/speechcraft/vad_percentile_rms_boundary_review_samples_2026-07-19"
)
SEED = 0
WINDOW_SEC = 0.35


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value: Any, default: float = 0.0) -> float:
    if value in ("", None):
        return default
    return float(value)


def add_review_window(row: dict[str, Any]) -> dict[str, Any]:
    timestamp = parse_float(row["timestamp_sec"])
    return {
        **row,
        "suggested_window_start_sec": round(max(0.0, timestamp - WINDOW_SEC), 6),
        "suggested_window_end_sec": round(timestamp + WINDOW_SEC, 6),
    }


def normalize_classification_row(
    row: dict[str, Any],
    *,
    detector_name: str,
    boundary_source: str,
    cutpoint_id: str,
) -> dict[str, Any]:
    out = {
        "speaker_id": row["speaker_id"],
        "recording_id": row["recording_id"],
        "buffer_id": row["buffer_id"],
        "detector_name": detector_name,
        "boundary_source": boundary_source,
        "cutpoint_id": cutpoint_id,
        "timestamp_sec": row["timestamp_sec"],
        "inside_target_speech_phone": row["inside_target_speech_phone"],
        "speech_phone_label": row["speech_phone_label"],
        "inside_annotated_silence": row["inside_annotated_silence"],
        "inside_laughter": row["inside_laughter"],
        "inside_vocnoise": row["inside_vocnoise"],
        "inside_noise": row["inside_noise"],
        "inside_interviewer": row["inside_interviewer"],
        "inside_uncertainty": row["inside_uncertainty"],
        "inside_boundary_marker": row["inside_boundary_marker"],
        "inside_other_annotation": row["inside_other_annotation"],
        "outside_all_annotations": row["outside_all_annotations"],
        "distance_to_nemo_start": row["distance_to_nemo_start"],
        "distance_to_nemo_end": row["distance_to_nemo_end"],
        "phone_start_sec": row["phone_start_sec"],
        "phone_end_sec": row["phone_end_sec"],
        "speech_phone_duration_sec": row["speech_phone_duration_sec"],
        "speech_phone_leak_depth_sec": row["speech_phone_leak_depth_sec"],
        "distance_from_phone_start_sec": row["distance_from_phone_start_sec"],
        "distance_from_phone_end_sec": row["distance_from_phone_end_sec"],
        "relative_leak_depth": row["relative_leak_depth"],
        "gt_20ms": row["gt_20ms"],
        "gt_50ms": row["gt_50ms"],
        "gt_100ms": row["gt_100ms"],
        "gt_25pct_phone": row["gt_25pct_phone"],
        "gt_50pct_phone": row["gt_50pct_phone"],
        "phone_duration_ms": round(parse_float(row["speech_phone_duration_sec"]) * 1000.0, 3)
        if row["speech_phone_duration_sec"] != ""
        else "",
        "leak_depth_ms": round(parse_float(row["speech_phone_leak_depth_sec"]) * 1000.0, 3)
        if row["speech_phone_leak_depth_sec"] != ""
        else "",
    }
    return out


def build_selected_unique_boundaries() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_cutpoint_ids: set[str] = set()
    for emitted_path in sorted(PER_RUN_ROOT.glob("*vad_percentile_rms/tables/emitted_clips.csv")):
        match = re.search(r"_buckeye_(s\d{2})_", emitted_path.parent.parent.name)
        if match is None:
            raise ValueError(f"could not infer speaker id from {emitted_path}")
        speaker_id = match.group(1)
        for clip in read_csv(emitted_path):
            endpoints = (
                ("detector_selected_cutpoint", clip["start_cutpoint_id"], clip["start_sec"]),
                ("detector_selected_cutpoint", clip["end_cutpoint_id"], clip["end_sec"]),
            )
            for boundary_source, cutpoint_id, timestamp_sec in endpoints:
                if cutpoint_id in seen_cutpoint_ids:
                    continue
                seen_cutpoint_ids.add(cutpoint_id)
                rows.append(
                    {
                        "speaker_id": speaker_id,
                        "recording_id": clip["recording_id"],
                        "buffer_id": clip["buffer_id"],
                        "cutpoint_id": cutpoint_id,
                        "timestamp_sec": float(timestamp_sec),
                    }
                )
    return rows


def classify_selected_unique_boundaries(boundaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping_csv = COHORT_ROOT / "benchmark_cluster_mapping.csv"
    boundaries_by_speaker: dict[str, list[dict[str, Any]]] = {}
    for row in boundaries:
        boundaries_by_speaker.setdefault(str(row["speaker_id"]), []).append(row)

    classified: list[dict[str, Any]] = []
    for speaker_id, speaker_rows in sorted(boundaries_by_speaker.items()):
        normalized_speaker_dir = COHORT_ROOT / "normalized" / speaker_id
        parser_artifacts = require_parser_artifacts(normalized_speaker_dir)
        reference_words = read_csv(parser_artifacts["reference_words"])
        reference_phones = read_csv(parser_artifacts["reference_phones"])
        interviewer_mask = build_interviewer_mask(
            reference_words=reference_words,
            reference_phones=reference_phones,
        )
        uncertainty_mask = load_uncertainty_masks_by_recording(parser_artifacts["uncertainty_masks"])
        annotation_rows = build_phone_annotations(
            reference_phones,
            interviewer_mask_by_recording=interviewer_mask,
            uncertainty_mask_by_recording=uncertainty_mask,
        )
        included_recording_ids, excluded_recording_ids = load_recording_eligibility(
            reference_reconciliation_csv=parser_artifacts["reference_reconciliation"],
            excluded_recordings_csv=parser_artifacts["excluded_recordings"],
        )
        eval_run = COHORT_ROOT / "eval_runs" / speaker_id
        cluster_mapping = load_nemo_cluster_mapping(mapping_csv, speaker_id=speaker_id)
        buffers = build_raw_nemo_target_buffers(
            speaker_id=speaker_id,
            eval_run=eval_run,
            cluster_mapping=cluster_mapping,
            included_recording_ids=included_recording_ids,
            excluded_recording_ids=excluded_recording_ids,
        )
        buffer_by_id = {buffer.buffer_id: buffer for buffer in buffers}
        for boundary in speaker_rows:
            buffer = buffer_by_id[boundary["buffer_id"]]
            classification = classify_boundary(
                speaker_id=speaker_id,
                recording_id=boundary["recording_id"],
                buffer=buffer,
                timestamp_sec=float(boundary["timestamp_sec"]),
                annotation_rows_by_recording=annotation_rows,
                interviewer_mask_by_recording=interviewer_mask,
                uncertainty_mask_by_recording=uncertainty_mask,
            )
            classified.append(
                normalize_classification_row(
                    classification,
                    detector_name="vad_percentile_rms",
                    boundary_source="detector_selected_cutpoint",
                    cutpoint_id=str(boundary["cutpoint_id"]),
                )
            )
    return classified


def choose_samples(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    rng = random.Random(SEED)
    target_phone_errors = [
        row
        for row in rows
        if row["inside_interviewer"] is False
        and row["inside_uncertainty"] is False
        and row["inside_target_speech_phone"] is True
    ]
    deepest_absolute = sorted(
        target_phone_errors,
        key=lambda row: (-parse_float(row["leak_depth_ms"]), str(row["cutpoint_id"])),
    )[:20]
    deepest_relative = sorted(
        target_phone_errors,
        key=lambda row: (-parse_float(row["relative_leak_depth"]), str(row["cutpoint_id"])),
    )[:20]
    shallow_pool = [
        row for row in target_phone_errors if 0.0 <= parse_float(row["leak_depth_ms"]) < 20.0
    ]
    rng.shuffle(shallow_pool)
    outside_target_pool = [
        row
        for row in rows
        if row["inside_interviewer"] is False
        and row["inside_uncertainty"] is False
        and row["inside_target_speech_phone"] is False
    ]
    rng.shuffle(outside_target_pool)
    non_problematic_pool = [
        row
        for row in outside_target_pool
        if row["inside_laughter"] is False
        and row["inside_vocnoise"] is False
        and row["inside_noise"] is False
        and row["inside_boundary_marker"] is False
        and row["inside_other_annotation"] is False
    ]
    rng.shuffle(non_problematic_pool)
    return {
        "deepest_absolute_errors": deepest_absolute,
        "deepest_relative_errors": deepest_relative,
        "random_shallow_errors_under_20ms": shallow_pool[:20],
        "random_boundaries_outside_target_phones": outside_target_pool[:20],
        "random_non_problematic_boundaries": non_problematic_pool[:20],
    }


def build_summary(rows: list[dict[str, Any]], samples: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    target_phone_errors = [
        row
        for row in rows
        if row["inside_interviewer"] is False
        and row["inside_uncertainty"] is False
        and row["inside_target_speech_phone"] is True
    ]
    outside_target = [
        row
        for row in rows
        if row["inside_interviewer"] is False
        and row["inside_uncertainty"] is False
        and row["inside_target_speech_phone"] is False
    ]
    non_problematic = [
        row
        for row in outside_target
        if row["inside_laughter"] is False
        and row["inside_vocnoise"] is False
        and row["inside_noise"] is False
        and row["inside_boundary_marker"] is False
        and row["inside_other_annotation"] is False
    ]
    return {
        "detector_name": "vad_percentile_rms",
        "random_seed": SEED,
        "selected_unique_boundary_count": len(rows),
        "target_phone_error_count": len(target_phone_errors),
        "outside_target_phone_count": len(outside_target),
        "non_problematic_boundary_count": len(non_problematic),
        "sample_bucket_counts": {name: len(bucket) for name, bucket in samples.items()},
        "outside_target_annotation_breakdown": dict(
            Counter(
                "outside_all_annotations"
                if row["outside_all_annotations"]
                else "annotated_silence"
                if row["inside_annotated_silence"]
                else "laughter"
                if row["inside_laughter"]
                else "vocnoise"
                if row["inside_vocnoise"]
                else "noise"
                if row["inside_noise"]
                else "boundary_marker"
                if row["inside_boundary_marker"]
                else "other_annotation"
                if row["inside_other_annotation"]
                else "other"
                for row in outside_target
            )
        ),
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    boundaries = build_selected_unique_boundaries()
    rows = classify_selected_unique_boundaries(boundaries)
    samples = choose_samples(rows)

    all_fieldnames = [
        "speaker_id",
        "recording_id",
        "buffer_id",
        "detector_name",
        "boundary_source",
        "cutpoint_id",
        "timestamp_sec",
        "inside_target_speech_phone",
        "speech_phone_label",
        "inside_annotated_silence",
        "inside_laughter",
        "inside_vocnoise",
        "inside_noise",
        "inside_interviewer",
        "inside_uncertainty",
        "inside_boundary_marker",
        "inside_other_annotation",
        "outside_all_annotations",
        "distance_to_nemo_start",
        "distance_to_nemo_end",
        "phone_start_sec",
        "phone_end_sec",
        "speech_phone_duration_sec",
        "speech_phone_leak_depth_sec",
        "distance_from_phone_start_sec",
        "distance_from_phone_end_sec",
        "relative_leak_depth",
        "gt_20ms",
        "gt_50ms",
        "gt_100ms",
        "gt_25pct_phone",
        "gt_50pct_phone",
        "phone_duration_ms",
        "leak_depth_ms",
    ]
    write_csv(OUTPUT_DIR / "all_selected_unique_boundary_classifications.csv", rows, all_fieldnames)

    sample_fieldnames = [
        *all_fieldnames,
        "suggested_window_start_sec",
        "suggested_window_end_sec",
    ]
    for name, bucket in samples.items():
        write_csv(
            OUTPUT_DIR / f"{name}.csv",
            [add_review_window(row) for row in bucket],
            sample_fieldnames,
        )

    summary = build_summary(rows, samples)
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "README.md").write_text(
        "\n".join(
            [
                "# vad_percentile_rms Boundary Review Samples",
                "",
                f"Random seed: `{SEED}`",
                "",
                "Bucket definitions:",
                "- `deepest_absolute_errors.csv`: top 20 selected unique boundaries by absolute leak depth in milliseconds.",
                "- `deepest_relative_errors.csv`: top 20 selected unique boundaries by relative leak depth within the phone.",
                "- `random_shallow_errors_under_20ms.csv`: 20 deterministic random in-phone errors with `0 <= leak_depth_ms < 20`.",
                "- `random_boundaries_outside_target_phones.csv`: 20 deterministic random selected unique boundaries that are not inside target speech phones, excluding interviewer and uncertainty intervals.",
                "- `random_non_problematic_boundaries.csv`: 20 deterministic random boundaries from the previous pool that are also not inside laughter, vocnoise, noise, boundary-marker, or other-annotation regions.",
                "",
                "All rows come from reconstructed unique selected cutpoints for `vad_percentile_rms`, using final emitted clip endpoints and the same repaired boundary classifier used by the benchmark.",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
