#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import shutil
import wave
from pathlib import Path
from typing import Any

SAMPLES_DIR = Path(
    "/home/aaravthegreat/Projects/speechcraft/vad_percentile_rms_boundary_review_samples_2026-07-19"
)
OUTPUT_DIR = Path(
    "/home/aaravthegreat/Projects/speechcraft/vad_percentile_rms_boundary_review_audio_2026-07-20"
)
COHORT_EVAL_RUNS = Path(
    "/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort/eval_runs"
)

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


def parse_float(value: str, default: float = 0.0) -> float:
    if value == "":
        return default
    return float(value)


def build_recording_audio_index() -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for manifest_path in sorted(COHORT_EVAL_RUNS.glob("s*/artifacts/source_audio_manifest.json")):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for row in data["sources"]:
            index[str(row["source_recording_id"])] = row
    return index


def copy_snippet(
    *,
    source_wav: Path,
    out_wav: Path,
    start_sec: float,
    end_sec: float,
) -> tuple[int, float]:
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


def main() -> None:
    audio_index = build_recording_audio_index()
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict[str, Any]] = []
    for bucket in BUCKETS:
        rows = read_csv(SAMPLES_DIR / f"{bucket}.csv")
        for idx, row in enumerate(rows, start=1):
            recording_id = row["recording_id"]
            source = audio_index[recording_id]
            source_wav = Path(source["path"])
            start_sec = parse_float(row["suggested_window_start_sec"])
            end_sec = parse_float(row["suggested_window_end_sec"])
            leak_ms = row["leak_depth_ms"] or "na"
            rel = row["relative_leak_depth"] or "na"
            filename = (
                f"{idx:02d}_{row['speaker_id']}_{recording_id}_{row['timestamp_sec']}s_"
                f"leak{leak_ms}ms_rel{rel}.wav"
            ).replace("/", "_")
            out_wav = OUTPUT_DIR / bucket / filename
            frame_count, clip_duration = copy_snippet(
                source_wav=source_wav,
                out_wav=out_wav,
                start_sec=start_sec,
                end_sec=end_sec,
            )
            manifest_rows.append(
                {
                    **row,
                    "bucket": bucket,
                    "source_wav_path": str(source_wav),
                    "clip_wav_path": str(out_wav),
                    "clip_frame_count": frame_count,
                    "clip_duration_sec": round(clip_duration, 6),
                }
            )

    fieldnames = [
        "bucket",
        "speaker_id",
        "recording_id",
        "buffer_id",
        "detector_name",
        "boundary_source",
        "cutpoint_id",
        "timestamp_sec",
        "speech_phone_label",
        "inside_target_speech_phone",
        "inside_annotated_silence",
        "inside_laughter",
        "inside_vocnoise",
        "inside_noise",
        "inside_boundary_marker",
        "inside_other_annotation",
        "outside_all_annotations",
        "phone_duration_ms",
        "leak_depth_ms",
        "relative_leak_depth",
        "suggested_window_start_sec",
        "suggested_window_end_sec",
        "source_wav_path",
        "clip_wav_path",
        "clip_frame_count",
        "clip_duration_sec",
    ]
    write_csv(OUTPUT_DIR / "manifest.csv", manifest_rows, fieldnames)
    shutil.copy2(SAMPLES_DIR / "README.md", OUTPUT_DIR / "README.md")
    shutil.copy2(SAMPLES_DIR / "summary.json", OUTPUT_DIR / "summary.json")


if __name__ == "__main__":
    main()
