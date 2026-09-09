from __future__ import annotations

import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .buffers import read_analysis_audio, sec_to_sample, write_pcm16_mono
from .io import read_json_value, resolve_under_root, sha256_file, write_json, write_jsonl
from .vr_slicer import (
    TRUSTED_GEOMETRY_FINGERPRINT,
    BufferScope,
    VR_O0_4,
    VrSlicerResult,
    assert_locked_geometry,
    slice_wav,
)


def assemble_candidate_review_clips(
    run_root: Path,
    config: dict[str, Any],
    *,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    destination_root = artifact_root or run_root
    sample_rate = int(config.get("analysis_sample_rate") or VR_O0_4.sample_rate_hz)
    if sample_rate != VR_O0_4.sample_rate_hz:
        raise ValueError(
            f"VR slicer requires analysis_sample_rate {VR_O0_4.sample_rate_hz}, got {sample_rate}"
        )
    fingerprint = assert_locked_geometry(VR_O0_4)
    buffers_path = resolve_under_root(run_root, "artifacts/processing_buffers.json")
    buffers = list(read_json_value(buffers_path))
    if not isinstance(buffers, list):
        raise ValueError("processing_buffers.json must contain a list")

    review_dir = resolve_under_root(destination_root, "artifacts/candidate_review_clips")
    if review_dir.exists():
        shutil.rmtree(review_dir)
    review_dir.mkdir(parents=True, exist_ok=True)

    by_source: dict[str, dict[str, Any]] = {}
    source_buffers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in buffers:
        if not isinstance(row, dict):
            raise ValueError("processing buffer row must be an object")
        source_audio_id = str(row.get("source_audio_id") or "")
        if not source_audio_id:
            raise ValueError("processing buffer is missing source_audio_id")
        audio_rel = str(row.get("analysis_audio_path") or row.get("audio_path") or "")
        if not audio_rel:
            raise ValueError(f"processing buffer {row.get('buffer_id')} is missing analysis audio path")
        existing = by_source.get(source_audio_id)
        if existing is None:
            by_source[source_audio_id] = {"audio_rel": audio_rel, "sample_rate": int(row.get("sample_rate") or sample_rate)}
        elif existing["audio_rel"] != audio_rel:
            raise ValueError(f"source {source_audio_id} has mixed analysis audio paths")
        source_buffers[source_audio_id].append(row)

    slicer_results: dict[str, VrSlicerResult] = {}
    for source_audio_id, source_rows in sorted(source_buffers.items()):
        meta = by_source[source_audio_id]
        audio_path = resolve_under_root(run_root, meta["audio_rel"])
        scopes = []
        for row in source_rows:
            start_sec = float(row.get("trusted_start_sec", row.get("source_start_sec")))
            end_sec = float(row.get("trusted_end_sec", row.get("source_end_sec")))
            if end_sec <= start_sec:
                continue
            scopes.append(
                BufferScope(
                    buffer_id=str(row["buffer_id"]),
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
            )
        if not scopes:
            continue
        slicer_results[source_audio_id] = slice_wav(
            audio_path,
            recording_id=source_audio_id,
            sample_rate_hz=int(meta["sample_rate"]),
            buffers=scopes,
            source_id=source_audio_id,
        )

    buffer_by_id = {str(row["buffer_id"]): row for row in buffers}
    packed: list[tuple[str, dict[str, Any], Any]] = []
    for source_audio_id in sorted(slicer_results):
        result = slicer_results[source_audio_id]
        for clip in result.clips:
            packed.append((source_audio_id, buffer_by_id[clip.buffer_id], clip))

    manifest: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for source_audio_id, source_rows in sorted(source_buffers.items()):
        if source_audio_id not in slicer_results or not slicer_results[source_audio_id].clips:
            for row in source_rows:
                rejected.append(
                    {
                        "buffer_id": row["buffer_id"],
                        "source_audio_id": source_audio_id,
                        "reason_codes": ["vr_slicer_emitted_no_clips"],
                    }
                )

    for source_audio_id, buffer, clip in packed:
        audio_rel = str(buffer.get("analysis_audio_path") or buffer["audio_path"])
        audio, actual_sample_rate = read_analysis_audio(resolve_under_root(run_root, audio_rel))
        if actual_sample_rate != sample_rate:
            raise ValueError(
                f"Candidate review audio sample-rate mismatch for {clip.buffer_id}: "
                f"{actual_sample_rate} != {sample_rate}"
            )
        start_sample = sec_to_sample(clip.start_sec, sample_rate)
        end_sample = sec_to_sample(clip.end_sec, sample_rate)
        if start_sample < 0 or end_sample > len(audio) or end_sample <= start_sample:
            raise RuntimeError(
                f"Invalid VR clip bounds for {clip.buffer_id}: {start_sample}:{end_sample}"
            )
        duration_sec = clip.duration_sec
        if duration_sec < VR_O0_4.min_clip_sec - 1e-6 or duration_sec > VR_O0_4.max_clip_sec + 1e-6:
            raise RuntimeError(
                f"VR clip duration outside locked bounds for {clip.clip_id}: {duration_sec}"
            )
        clip_id = f"candidate_review_clip_{len(manifest):06d}"
        rel_audio_path = f"artifacts/candidate_review_clips/{clip_id}.wav"
        clip_path = resolve_under_root(destination_root, rel_audio_path)
        write_pcm16_mono(clip_path, audio[start_sample:end_sample], sample_rate)
        audio_sha256 = sha256_file(clip_path)
        trusted_start = int(buffer.get("trusted_start_sample") or buffer.get("source_start_sample") or 0)
        manifest.append(
            {
                "id": clip_id,
                "buffer_id": clip.buffer_id,
                "source_audio_id": source_audio_id,
                "audio_path": rel_audio_path,
                "audio_sha256": audio_sha256,
                "audio_hash": audio_sha256,
                "sample_rate": sample_rate,
                "start_cutpoint_ref": clip.start_cutpoint_id,
                "end_cutpoint_ref": clip.end_cutpoint_id,
                "buffer_local_start_sample": start_sample - trusted_start,
                "buffer_local_end_sample": end_sample - trusted_start,
                "source_start_sample": start_sample,
                "source_end_sample": end_sample,
                "duration_samples": end_sample - start_sample,
                "duration_sec": round((end_sample - start_sample) / sample_rate, 6),
                "slicer": "VR",
                "slicer_geometry": "O0_4",
                "geometry_fingerprint": fingerprint,
                "training_text": "",
                "needs_review": False,
                "review_reason_codes": [],
                "status": "candidate_review",
            }
        )

    cutpoints_payload = []
    for source_audio_id in sorted(slicer_results):
        for cut in slicer_results[source_audio_id].selected_cutpoints:
            cutpoints_payload.append(
                {
                    "id": cut.cutpoint_id,
                    "buffer_id": cut.buffer_id,
                    "source_audio_id": source_audio_id,
                    "time_sec": cut.time_sec,
                    "interval_start_sec": cut.interval_start_sec,
                    "interval_end_sec": cut.interval_end_sec,
                    "score": cut.score,
                    "detector_name": cut.detector_name,
                }
            )
    cutpoints_path = resolve_under_root(destination_root, "artifacts/vr_cutpoints.jsonl")
    write_jsonl(cutpoints_path, cutpoints_payload)

    manifest_path = resolve_under_root(destination_root, "artifacts/candidate_review_manifest.json")
    rejected_path = resolve_under_root(destination_root, "artifacts/candidate_review_rejected.json")
    write_json(manifest_path, manifest)
    write_json(rejected_path, rejected)
    durations = [float(row["duration_sec"]) for row in manifest]
    summary = {
        "stage": "candidate_review_clips",
        "slicer": "VR",
        "slicer_geometry": "O0_4",
        "geometry_fingerprint": fingerprint,
        "trusted_geometry_fingerprint": TRUSTED_GEOMETRY_FINGERPRINT,
        "config_hash": str(config.get("config_hash") or ""),
        "input_artifact_hashes": {
            "processing_buffers_json": sha256_file(buffers_path),
        },
        "output_hashes": {
            "candidate_review_manifest_json": sha256_file(manifest_path),
            "candidate_review_rejected_json": sha256_file(rejected_path),
            "vr_cutpoints_jsonl": sha256_file(cutpoints_path),
            "candidate_review_wavs": {row["id"]: row["audio_sha256"] for row in manifest},
        },
        "candidate_review_clips": len(manifest),
        "rejected_spans": len(rejected),
        "total_duration_sec": round(sum(durations), 6),
        "min_clip_duration_sec": min(durations, default=None),
        "max_clip_duration_sec": max(durations, default=None),
        "clips_needing_review": 0,
        "review_reason_counts": {},
        "rejection_reason_counts": dict(
            sorted(
                Counter(reason for row in rejected for reason in row["reason_codes"]).items()
            )
        ),
        "thresholds": {
            "min_clip_sec": VR_O0_4.min_clip_sec,
            "preferred_min_sec": VR_O0_4.preferred_min_sec,
            "preferred_max_sec": VR_O0_4.preferred_max_sec,
            "target_clip_sec": VR_O0_4.target_clip_sec,
            "max_clip_sec": VR_O0_4.max_clip_sec,
        },
        "output_dir": "artifacts/candidate_review_clips",
    }
    write_json(resolve_under_root(destination_root, "artifacts/candidate_review_summary.json"), summary)
    return summary
