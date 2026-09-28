#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MEDIA_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/backend/data/media")
DB_PATH = Path("/home/aaravthegreat/Projects/speechcraft/backend/data/project.db")


def utc_sql() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def wav_info(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        sample_rate = reader.getframerate()
        return {
            "duration_sec": frames / sample_rate if sample_rate else 0.0,
            "sample_rate": sample_rate,
            "channels": reader.getnchannels(),
            "num_samples": frames,
        }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def artifact_hash(path: Path) -> str:
    return f"sha256:{sha256_file(path)}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", default="emmawatsonv2")
    parser.add_argument("--project-name", default="EmmaWatsonV2")
    parser.add_argument("--run-id", default="dataset-emmawatsonv2-cliplab")
    parser.add_argument("--slicer-run", required=True)
    args = parser.parse_args()

    slicer_run = Path(args.slicer_run)
    clips_dir = slicer_run / "emitted_audio_clips"
    transcripts_path = slicer_run / "asr_large_v3" / "clip_transcripts.csv"
    emitted_path = slicer_run / "tables" / "emitted_clips.csv"
    if not clips_dir.exists():
        raise FileNotFoundError(clips_dir)
    if not transcripts_path.exists():
        raise FileNotFoundError(transcripts_path)
    if not emitted_path.exists():
        raise FileNotFoundError(emitted_path)

    transcripts = {row["clip_id"]: row for row in read_csv(transcripts_path)}
    emitted = {row["clip_id"]: row for row in read_csv(emitted_path)}
    clip_paths = sorted(clips_dir.glob("*.wav"))
    if not clip_paths:
        raise ValueError(f"no wav clips found in {clips_dir}")

    artifact_root = f"dataset-runs/{args.project_id}/{args.run_id}"
    run_root = MEDIA_ROOT / artifact_root
    artifacts_dir = run_root / "artifacts"
    review_clips_dir = artifacts_dir / "candidate_review_clips"
    if run_root.exists():
        shutil.rmtree(run_root)
    review_clips_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    total_duration = 0.0
    total_words = 0
    for index, source_clip_path in enumerate(clip_paths):
        original_clip_id = source_clip_path.stem
        clip_id = f"candidate_review_clip_{index:06d}"
        out_name = f"{clip_id}.wav"
        out_path = review_clips_dir / out_name
        shutil.copy2(source_clip_path, out_path)
        info = wav_info(out_path)
        duration_sec = round(float(info["duration_sec"]), 6)
        sample_rate = int(info["sample_rate"])
        digest = sha256_file(out_path)
        transcript_row = transcripts.get(original_clip_id, {})
        emitted_row = emitted.get(original_clip_id, {})
        text = str(transcript_row.get("text") or "").strip()
        word_count = int(float(transcript_row.get("word_count") or 0))
        total_duration += duration_sec
        total_words += word_count
        manifest.append(
            {
                "id": clip_id,
                "status": "candidate_review",
                "audio_path": f"artifacts/candidate_review_clips/{out_name}",
                "audio_sha256": f"sha256:{digest}",
                "audio_hash": f"sha256:{digest}",
                "sample_rate": sample_rate,
                "duration_samples": int(info["num_samples"]),
                "duration_sec": duration_sec,
                "training_text": text,
                "alignment_text": text.lower(),
                "needs_review": False,
                "review_reason_codes": [],
                "source_audio_id": "youtube_u6iJN8OovWg_speaker_0",
                "buffer_id": emitted_row.get("buffer_id") or "",
                "start_cutpoint_ref": emitted_row.get("start_cutpoint_id") or "",
                "end_cutpoint_ref": emitted_row.get("end_cutpoint_id") or "",
                "source_start_sample": int(round(float(emitted_row.get("start_sec") or 0.0) * sample_rate)),
                "source_end_sample": int(round(float(emitted_row.get("end_sec") or 0.0) * sample_rate)),
                "source_start_sec": float(emitted_row.get("start_sec") or 0.0),
                "source_end_sec": float(emitted_row.get("end_sec") or 0.0),
                "original_slicer_clip_id": original_clip_id,
                "asr_model": "Systran/faster-whisper-large-v3",
                "asr_word_count": word_count,
            }
        )

    manifest_path = artifacts_dir / "candidate_review_manifest.json"
    summary_path = artifacts_dir / "candidate_review_summary.json"
    status_path = run_root / "status.json"
    config_path = run_root / "config.json"
    write_json(manifest_path, manifest)
    write_json(
        summary_path,
        {
            "stage": "candidate_review_clips",
            "clip_count": len(manifest),
            "total_duration_sec": round(total_duration, 6),
            "total_words": total_words,
            "source": "external_vad_percentile_rms_slicer_plus_large_v3_asr",
            "qc_artifacts_included": False,
        },
    )
    write_json(
        config_path,
        {
            "project_name": args.project_name,
            "source_slicer_run": str(slicer_run),
            "detector": "vad_percentile_rms",
            "asr_model": "Systran/faster-whisper-large-v3",
            "clip_lab_only_import": True,
            "qc_page_artifacts": False,
        },
    )
    write_json(
        status_path,
        {
            "ok": True,
            "stage": "candidate_review_clips",
            "reason": "clip_lab_only_import",
            "clip_count": len(manifest),
            "total_duration_sec": round(total_duration, 6),
        },
    )
    shutil.copy2(transcripts_path, artifacts_dir / "large_v3_clip_transcripts.csv")
    shutil.copy2(slicer_run / "asr_large_v3" / "word_probabilities.csv", artifacts_dir / "large_v3_word_probabilities.csv")
    shutil.copy2(slicer_run / "tables" / "emitted_clips.csv", artifacts_dir / "slicer_emitted_clips.csv")

    created_at = utc_sql()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        existing = conn.execute("select id from importbatch where id = ?", (args.project_id,)).fetchone()
        if existing is None:
            conn.execute(
                "insert into importbatch (id, name, created_at, active_prepared_output_group_id, active_preparation_job_id) values (?, ?, ?, null, null)",
                (args.project_id, args.project_name, created_at),
            )
        else:
            conn.execute("update importbatch set name = ? where id = ?", (args.project_name, args.project_id))
        conn.execute("delete from runartifact where run_id = ?", (args.run_id,))
        conn.execute("delete from processingrun where id = ?", (args.run_id,))
        conn.execute(
            """
            insert into processingrun (
              id, project_id, pipeline_version, stage, status, config_hash,
              input_summary, output_summary, reason_codes, created_at, started_at, completed_at, artifact_root
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.run_id,
                args.project_id,
                "external_clip_lab_import_v1",
                "candidate_clips",
                "completed",
                artifact_hash(config_path),
                json.dumps({"source_slicer_run": str(slicer_run), "clip_count": len(manifest)}),
                json.dumps({"clip_count": len(manifest), "total_duration_sec": round(total_duration, 6), "total_words": total_words}),
                json.dumps(["clip_lab_only_import", "qc_artifacts_omitted"]),
                created_at,
                created_at,
                created_at,
                artifact_root,
            ),
        )
        for rel_path, kind, summary in (
            ("config.json", "run_config_json", {}),
            ("status.json", "run_status_json", {"clip_count": len(manifest)}),
            ("artifacts/candidate_review_manifest.json", "candidate_review_manifest_json", {"clip_count": len(manifest)}),
            ("artifacts/candidate_review_summary.json", "candidate_review_summary_json", {"clip_count": len(manifest), "total_duration_sec": round(total_duration, 6)}),
        ):
            path = run_root / rel_path
            conn.execute(
                """
                insert into runartifact (
                  id, run_id, project_id, source_audio_id, source_recording_id, kind, path,
                  schema_version, byte_size, content_hash, config_hash, backend, backend_version,
                  status, summary, reason_codes, created_at, input_artifact_hashes
                ) values (?, ?, ?, null, null, ?, ?, 1, ?, ?, ?, ?, null, ?, ?, ?, ?, ?)
                """,
                (
                    f"{args.run_id}:{kind}",
                    args.run_id,
                    args.project_id,
                    kind,
                    rel_path,
                    path.stat().st_size,
                    artifact_hash(path),
                    artifact_hash(config_path),
                    "external_clip_lab_import",
                    "materialized",
                    json.dumps(summary),
                    json.dumps([]),
                    created_at,
                    json.dumps({}),
                ),
            )
        conn.commit()

    print(json.dumps({
        "project_id": args.project_id,
        "project_name": args.project_name,
        "run_id": args.run_id,
        "run_root": str(run_root),
        "clip_count": len(manifest),
        "total_duration_sec": round(total_duration, 6),
        "total_words": total_words,
        "candidate_manifest": str(manifest_path),
    }, indent=2))


if __name__ == "__main__":
    main()
