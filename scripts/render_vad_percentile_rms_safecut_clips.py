#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import random
import re
import shutil
import wave
from pathlib import Path
from typing import Any

ROOT = Path(
    "/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1"
)
MATRIX_ROOT = ROOT / "full" / (
    "2026-07-17_000000_buckeye_s01_s02_s03_s04_s05_s06_s07_s08_s09_s10_s11_s12_s13_"
    "s14_s15_s16_s17_s18_s19_s20_s21_s22_s24_repaired_matrix"
)
PER_RUN_ROOT = MATRIX_ROOT / "per_run"
COHORT_EVAL_RUNS = ROOT / "cohort" / "eval_runs"
CLASSIFICATION_DIR = Path(
    "/home/aaravthegreat/Projects/speechcraft/vad_percentile_rms_boundary_review_samples_2026-07-19"
)
OUTPUT_DIR = Path(
    "/home/aaravthegreat/Projects/speechcraft/vad_percentile_rms_safecut_clip_review_2026-07-20"
)
SEED = 0

BUCKETS = [
    "deepest_absolute_errors",
    "deepest_relative_errors",
    "random_shallow_errors_under_20ms",
    "random_boundaries_outside_target_phones",
    "random_non_problematic_boundaries",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def parse_float(value: Any, default: float = 0.0) -> float:
    if value in ("", None):
        return default
    return float(value)


def build_audio_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(COHORT_EVAL_RUNS.glob("s*/artifacts/source_audio_manifest.json")):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in data["sources"]:
            index[str(row["source_recording_id"])] = row
    return index


def build_clip_endpoint_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    audio_index = build_audio_index()
    for emitted_path in sorted(PER_RUN_ROOT.glob("*vad_percentile_rms/tables/emitted_clips.csv")):
        match = re.search(r"_buckeye_(s\d{2})_", emitted_path.parent.parent.name)
        if match is None:
            raise ValueError(f"could not infer speaker id from {emitted_path}")
        speaker_id = match.group(1)
        for clip in read_csv(emitted_path):
            recording_id = clip["recording_id"]
            source = audio_index[recording_id]
            common = {
                "speaker_id": speaker_id,
                "recording_id": recording_id,
                "buffer_id": clip["buffer_id"],
                "clip_id": clip["clip_id"],
                "clip_start_sec": clip["start_sec"],
                "clip_end_sec": clip["end_sec"],
                "clip_duration_sec": clip["duration_sec"],
                "source_wav_path": source["path"],
            }
            rows.append(
                {
                    **common,
                    "boundary_role": "start",
                    "cutpoint_id": clip["start_cutpoint_id"],
                    "timestamp_sec": clip["start_sec"],
                }
            )
            rows.append(
                {
                    **common,
                    "boundary_role": "end",
                    "cutpoint_id": clip["end_cutpoint_id"],
                    "timestamp_sec": clip["end_sec"],
                }
            )
    return rows


def build_classification_index() -> dict[str, dict[str, str]]:
    rows = read_csv(CLASSIFICATION_DIR / "all_selected_unique_boundary_classifications.csv")
    return {row["cutpoint_id"]: row for row in rows}


def joined_boundary_rows() -> list[dict[str, Any]]:
    class_index = build_classification_index()
    joined: list[dict[str, Any]] = []
    for row in build_clip_endpoint_rows():
        classification = class_index.get(row["cutpoint_id"])
        if classification is None:
            raise ValueError(f"missing classification row for {row['cutpoint_id']}")
        joined.append({**classification, **row})
    return joined


def copy_clip(source_wav: Path, out_wav: Path, start_sec: float, end_sec: float) -> tuple[int, float]:
    with wave.open(str(source_wav), "rb") as reader:
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        total_frames = reader.getnframes()
        start_frame = max(0, min(total_frames, int(round(start_sec * sample_rate))))
        end_frame = max(start_frame, min(total_frames, int(round(end_sec * sample_rate))))
        reader.setpos(start_frame)
        frames = reader.readframes(end_frame - start_frame)
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(sample_width)
            writer.setframerate(sample_rate)
            writer.writeframes(frames)
    duration = (end_frame - start_frame) / sample_rate if sample_rate else 0.0
    return end_frame - start_frame, duration


def is_target_phone_error(row: dict[str, Any]) -> bool:
    return (
        row["inside_interviewer"] == "False"
        and row["inside_uncertainty"] == "False"
        and row["inside_target_speech_phone"] == "True"
    )


def is_outside_target_phone(row: dict[str, Any]) -> bool:
    return (
        row["inside_interviewer"] == "False"
        and row["inside_uncertainty"] == "False"
        and row["inside_target_speech_phone"] == "False"
    )


def is_non_problematic(row: dict[str, Any]) -> bool:
    return (
        is_outside_target_phone(row)
        and row["inside_laughter"] == "False"
        and row["inside_vocnoise"] == "False"
        and row["inside_noise"] == "False"
        and row["inside_boundary_marker"] == "False"
        and row["inside_other_annotation"] == "False"
    )


def choose_bucket_rows(rows: list[dict[str, Any]], *, role: str, bucket: str, rng: random.Random) -> list[dict[str, Any]]:
    role_rows = [row for row in rows if row["boundary_role"] == role]
    if bucket == "deepest_absolute_errors":
        pool = [row for row in role_rows if is_target_phone_error(row)]
        return sorted(pool, key=lambda row: (-parse_float(row["leak_depth_ms"]), row["cutpoint_id"]))[:10]
    if bucket == "deepest_relative_errors":
        pool = [row for row in role_rows if is_target_phone_error(row)]
        return sorted(pool, key=lambda row: (-parse_float(row["relative_leak_depth"]), row["cutpoint_id"]))[:10]
    if bucket == "random_shallow_errors_under_20ms":
        pool = [
            row
            for row in role_rows
            if is_target_phone_error(row) and 0.0 <= parse_float(row["leak_depth_ms"]) < 20.0
        ]
        rng.shuffle(pool)
        return pool[:10]
    if bucket == "random_boundaries_outside_target_phones":
        pool = [row for row in role_rows if is_outside_target_phone(row)]
        rng.shuffle(pool)
        return pool[:10]
    if bucket == "random_non_problematic_boundaries":
        pool = [row for row in role_rows if is_non_problematic(row)]
        rng.shuffle(pool)
        return pool[:10]
    raise ValueError(bucket)


def bucket_filename(index: int, row: dict[str, Any]) -> str:
    leak = row["leak_depth_ms"] or "na"
    rel = row["relative_leak_depth"] or "na"
    return (
        f"{index:02d}_{row['speaker_id']}_{row['recording_id']}_{row['boundary_role']}_"
        f"{row['timestamp_sec']}s_leak{leak}ms_rel{rel}.wav"
    ).replace("/", "_")


def main() -> None:
    rows = joined_boundary_rows()
    rng = random.Random(SEED)
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict[str, Any]] = []
    bucket_counts: dict[str, dict[str, int]] = {}
    for bucket in BUCKETS:
        bucket_counts[bucket] = {}
        for role in ("start", "end"):
            selected = choose_bucket_rows(rows, role=role, bucket=bucket, rng=rng)
            if len(selected) != 10:
                raise ValueError(f"{bucket} {role} only produced {len(selected)} rows")
            bucket_counts[bucket][role] = len(selected)
            for idx, row in enumerate(selected, start=1):
                out_wav = OUTPUT_DIR / bucket / role / bucket_filename(idx, row)
                frame_count, clip_duration = copy_clip(
                    Path(row["source_wav_path"]),
                    out_wav,
                    parse_float(row["clip_start_sec"]),
                    parse_float(row["clip_end_sec"]),
                )
                manifest_rows.append(
                    {
                        **row,
                        "bucket": bucket,
                        "clip_wav_path": str(out_wav),
                        "clip_frame_count": frame_count,
                        "rendered_clip_duration_sec": round(clip_duration, 6),
                    }
                )

    manifest_fieldnames = [
        "bucket",
        "boundary_role",
        "speaker_id",
        "recording_id",
        "buffer_id",
        "clip_id",
        "cutpoint_id",
        "timestamp_sec",
        "clip_start_sec",
        "clip_end_sec",
        "clip_duration_sec",
        "rendered_clip_duration_sec",
        "speech_phone_label",
        "leak_depth_ms",
        "relative_leak_depth",
        "inside_target_speech_phone",
        "inside_annotated_silence",
        "inside_laughter",
        "inside_vocnoise",
        "inside_noise",
        "inside_boundary_marker",
        "inside_other_annotation",
        "outside_all_annotations",
        "source_wav_path",
        "clip_wav_path",
        "clip_frame_count",
    ]
    write_csv(OUTPUT_DIR / "manifest.csv", manifest_rows, manifest_fieldnames)

    summary = {
        "detector_name": "vad_percentile_rms",
        "random_seed": SEED,
        "bucket_counts": bucket_counts,
        "total_rendered_clips": len(manifest_rows),
        "notes": [
            "Each bucket contains 20 real emitted SafeCut clips.",
            "Each bucket is split into 10 clips where the sampled boundary is the clip start and 10 where it is the clip end.",
            "Clips are rendered from the original source WAV over the full emitted clip duration, not a short boundary-centered window.",
        ],
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "README.md").write_text(
        "\n".join(
            [
                "# vad_percentile_rms SafeCut Clip Review",
                "",
                "These are full emitted clips from the final benchmark, not short boundary windows.",
                "",
                "Per bucket:",
                "- `start/`: 10 clips where the sampled boundary is the clip start.",
                "- `end/`: 10 clips where the sampled boundary is the clip end.",
                "",
                "Buckets:",
                "- `deepest_absolute_errors`",
                "- `deepest_relative_errors`",
                "- `random_shallow_errors_under_20ms`",
                "- `random_boundaries_outside_target_phones`",
                "- `random_non_problematic_boundaries`",
            ]
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
