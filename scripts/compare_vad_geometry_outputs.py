#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import wave
import zipfile
from pathlib import Path
from typing import Any

RUN_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs")
BUNDLE = RUN_ROOT / "vad_geometry_comparison_review_bundle_2026-07-20.zip"
COMPARISON_ROOT = RUN_ROOT / "vad_geometry_comparison_2026-07-20"

RUNS = {
    "poki2": {
        "source": Path("/home/aaravthegreat/Projects/Charactors/Pokimane/raw/poki2.wav"),
        "A_current": RUN_ROOT / "vad_percentile_rms_poki2_onnx_optimized_2026-07-20",
        "B_standard_512": RUN_ROOT / "vad_percentile_rms_poki2_onnx_geomB_512_512_o0_2026-07-20",
        "C_two_stream_512": RUN_ROOT / "vad_percentile_rms_poki2_onnx_geomC_512_512_o0-256_2026-07-20",
    },
    "Mckenna1": {
        "source": Path("/home/aaravthegreat/Projects/Charactors/MckennaGrace/raw/Mckenna1.wav"),
        "A_current": RUN_ROOT / "vad_percentile_rms_mckenna1_onnx_optimized_2026-07-20",
        "B_standard_512": RUN_ROOT / "vad_percentile_rms_mckenna1_onnx_geomB_512_512_o0_2026-07-20",
        "C_two_stream_512": RUN_ROOT / "vad_percentile_rms_mckenna1_onnx_geomC_512_512_o0-256_2026-07-20",
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


def read_runtime(run: Path) -> dict[str, dict[str, str]]:
    return {row["stage"]: row for row in read_csv(run / "tables" / "runtime_breakdown.csv")}


def read_provenance(run: Path) -> dict[str, Any]:
    return json.loads((run / "provenance.json").read_text(encoding="utf-8"))


def clip_key(row: dict[str, str]) -> tuple[int, int]:
    return (int(round(float(row["start_sec"]) * 1000.0)), int(round(float(row["end_sec"]) * 1000.0)))


def cutpoint_structural_key(row: dict[str, str]) -> tuple[str, str, int, int, int]:
    return (
        row["recording_id"],
        row["buffer_id"],
        int(round(float(row["time_sec"]) * 1000.0)),
        int(round(float(row["interval_start_sec"]) * 1000.0)),
        int(round(float(row["interval_end_sec"]) * 1000.0)),
    )


def clip_structural_key(row: dict[str, str]) -> tuple[str, str, int, int]:
    return (
        row["recording_id"],
        row["buffer_id"],
        int(round(float(row["start_sec"]) * 1000.0)),
        int(round(float(row["end_sec"]) * 1000.0)),
    )


def full_row_set(rows: list[dict[str, str]]) -> set[str]:
    return {json.dumps(row, sort_keys=True) for row in rows}


def copy_clip(source: Path, out_path: Path, start_sec: float, end_sec: float) -> None:
    with wave.open(str(source), "rb") as reader:
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        total_frames = reader.getnframes()
        start_frame = max(0, min(total_frames, int(round(start_sec * sample_rate))))
        end_frame = max(start_frame, min(total_frames, int(round(end_sec * sample_rate))))
        reader.setpos(start_frame)
        frames = reader.readframes(end_frame - start_frame)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(frames)


def main() -> None:
    if COMPARISON_ROOT.exists():
        import shutil

        shutil.rmtree(COMPARISON_ROOT)
    COMPARISON_ROOT.mkdir(parents=True)
    summary_rows: list[dict[str, Any]] = []
    changed_clip_rows: list[dict[str, Any]] = []
    for recording_id, paths in RUNS.items():
        source = paths["source"]
        reference = paths["A_current"]
        ref_cuts = read_csv(reference / "tables" / "selected_cutpoints.csv")
        ref_clips = read_csv(reference / "tables" / "emitted_clips.csv")
        ref_runtime = read_runtime(reference)
        ref_prov = read_provenance(reference)
        ref_clip_by_key = {clip_key(row): row for row in ref_clips}
        for geometry, run in paths.items():
            if geometry == "source" or geometry == "A_current":
                continue
            rows = read_csv(run / "tables" / "selected_cutpoints.csv")
            clips = read_csv(run / "tables" / "emitted_clips.csv")
            runtime = read_runtime(run)
            prov = read_provenance(run)
            clip_by_key = {clip_key(row): row for row in clips}
            changed_clip_keys = sorted(set(ref_clip_by_key) ^ set(clip_by_key))
            for key in changed_clip_keys:
                for label, table in (("A_current", ref_clip_by_key), (geometry, clip_by_key)):
                    row = table.get(key)
                    if row is None:
                        continue
                    out = COMPARISON_ROOT / "changed_clip_audio" / recording_id / geometry / label / f"{label}_{row['start_sec']}_{row['end_sec']}.wav"
                    copy_clip(source, out, float(row["start_sec"]), float(row["end_sec"]))
                    changed_clip_rows.append(
                        {
                            "recording_id": recording_id,
                            "geometry": geometry,
                            "source_geometry": label,
                            "clip_id": row["clip_id"],
                            "start_sec": row["start_sec"],
                            "end_sec": row["end_sec"],
                            "duration_sec": row["duration_sec"],
                            "audio_path": str(out),
                        }
                    )
            summary_rows.append(
                {
                    "recording_id": recording_id,
                    "geometry": geometry,
                    "reference_geometry": "A_current",
                    "vad_calls_reference": ref_prov["vad_inference_call_count"],
                    "vad_calls_geometry": prov["vad_inference_call_count"],
                    "vad_runtime_reference_sec": ref_runtime["vad_frame_extraction"]["wall_clock_sec"],
                    "vad_runtime_geometry_sec": runtime["vad_frame_extraction"]["wall_clock_sec"],
                    "total_runtime_reference_sec": ref_runtime["total_run"]["wall_clock_sec"],
                    "total_runtime_geometry_sec": runtime["total_run"]["wall_clock_sec"],
                    "selected_cutpoints_reference": len(ref_cuts),
                    "selected_cutpoints_geometry": len(rows),
                    "cutpoint_structural_differences": len({cutpoint_structural_key(row) for row in ref_cuts} ^ {cutpoint_structural_key(row) for row in rows}),
                    "cutpoint_full_row_differences": len(full_row_set(ref_cuts) ^ full_row_set(rows)),
                    "emitted_clips_reference": len(ref_clips),
                    "emitted_clips_geometry": len(clips),
                    "clip_structural_differences": len({clip_structural_key(row) for row in ref_clips} ^ {clip_structural_key(row) for row in clips}),
                    "clip_full_row_differences": len(full_row_set(ref_clips) ^ full_row_set(clips)),
                    "retained_duration_reference_sec": round(sum(float(row["duration_sec"]) for row in ref_clips), 6),
                    "retained_duration_geometry_sec": round(sum(float(row["duration_sec"]) for row in clips), 6),
                    "invariant_failures_geometry": sum(int(row["failed_count"]) for row in read_csv(run / "tables" / "invariant_report.csv")),
                }
            )
    write_csv(COMPARISON_ROOT / "vad_geometry_comparison_summary.csv", summary_rows, list(summary_rows[0].keys()))
    write_csv(
        COMPARISON_ROOT / "changed_clip_review_manifest.csv",
        changed_clip_rows,
        ["recording_id", "geometry", "source_geometry", "clip_id", "start_sec", "end_sec", "duration_sec", "audio_path"],
    )
    if BUNDLE.exists():
        BUNDLE.unlink()
    with zipfile.ZipFile(BUNDLE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(COMPARISON_ROOT.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(COMPARISON_ROOT.parent))
        zf.write(Path(__file__), Path("code") / Path(__file__).name)
        zf.write(Path("/home/aaravthegreat/Projects/speechcraft/scripts/run_personal_vad_percentile_rms.py"), Path("code") / "run_personal_vad_percentile_rms.py")
    print(json.dumps({"comparison_root": str(COMPARISON_ROOT), "bundle": str(BUNDLE), "rows": summary_rows}, indent=2))


if __name__ == "__main__":
    main()
