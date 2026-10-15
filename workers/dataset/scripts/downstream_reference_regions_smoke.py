#!/usr/bin/env python3
"""Opt-in real downstream pipeline smoke using explicitly test-only speaker labels.

This harness replaces only the diarization stage with the supplied annotation
for the bundled CC0 fixture. It is not a production diarization substitute.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--source-wav", default=str(WORKER_ROOT / "runtime-fixtures" / "speech.wav"))
    args = parser.parse_args()
    if os.environ.get("SPEECHCRAFT_TEST_ONLY_REFERENCE_REGIONS") != "1":
        parser.error("Set SPEECHCRAFT_TEST_ONLY_REFERENCE_REGIONS=1 to enable the test-only annotation harness.")
    if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") != "1":
        parser.error("This smoke must run in a controller-managed worker environment.")

    import soundfile as sf
    from speechcraft_dataset.diarization import _write_diarization_artifacts
    from speechcraft_dataset.io import read_json, write_json
    import speechcraft_dataset.run as worker

    run_root = Path(args.run_root).resolve()
    source = Path(args.source_wav).resolve()
    audio_info = sf.info(str(source))
    if audio_info.duration < 3.0 or audio_info.channels < 1:
        parser.error("Reference fixture must contain at least three seconds of audio.")

    def reference_regions(root: Path, config: dict) -> dict:
        variants = list(read_json(root / "artifacts/audio_variants_manifest.json").get("variants") or [])
        rows = []
        for variant in variants:
            sample_rate = int(variant["sample_rate"])
            sample_count = int(variant["num_samples"])
            rows.append({"id": f"reference-speaker-{variant['source_audio_id']}",
                "speaker_id": "speaker_0", "source_audio_id": variant["source_audio_id"],
                "analysis_audio_path": variant["path"], "start_sec": 0.0,
                "end_sec": round(sample_count / sample_rate, 6), "start_sample": 0,
                "end_sample": sample_count, "confidence": 1.0,
                "backend": "test_only_reference_annotation"})
        summary = _write_diarization_artifacts(root, config, rows,
            sample_rate_by_source={str(row["source_audio_id"]): int(row["sample_rate"]) for row in variants},
            analysis_path_by_source={str(row["source_audio_id"]): str(row["path"]) for row in variants},
            sample_count=1, sample_duration_sec=6.0, backend="test_only_reference_annotation",
            backend_version="fixture-annotation-v1", mode="diarization", extra={
                "test_only": True, "annotation_source": "single-speaker interval over CC0 fixture"},
            input_hashes={"audio_variants_manifest": "test-only-reference-regions"},
            auto_select_speaker_0=False)
        write_json(root / "artifacts/speaker_selection.json", {"mode": "diarization", "selected": True,
            "target_speaker_id": "speaker_0", "source": "test_only_reference_annotation",
            "available_speaker_ids": ["speaker_0"], "updated_at": None})
        summary["reason_codes"] = []
        summary["test_only_reference_regions"] = True
        return summary

    worker.run_diarization = reference_regions
    worker_args = argparse.Namespace(run_root=str(run_root), source_wav=[str(source)], config=None,
        single_speaker=False, target_speaker_label="speaker_0", stop_after="native_export")
    started = time.monotonic()
    code = worker.run_dataset_worker(worker_args)
    if code != 0:
        print(json.dumps({"schema_version": 1, "result": "failed", "test_only_reference_regions": True,
            "runtime_seconds": round(time.monotonic() - started, 3),
            "status": json.loads((run_root / "status.json").read_text())}, sort_keys=True))
        return code

    artifacts = run_root / "artifacts"
    candidates = json.loads((artifacts / "candidate_review_manifest.json").read_text())
    transcript = json.loads((artifacts / "transcript_qc.json").read_text())
    transcript_summary = json.loads((artifacts / "transcript_qc_summary.json").read_text())
    purity_path = artifacts / "speaker_purity.json"
    purity = json.loads(purity_path.read_text()) if purity_path.is_file() else {}
    purity_summary = json.loads((artifacts / "speaker_purity_summary.json").read_text()) if (artifacts / "speaker_purity_summary.json").is_file() else {}
    review_summary = json.loads((artifacts / "candidate_review_summary.json").read_text())
    exports = json.loads((artifacts / "export_manifest.json").read_text())
    transcript_rows = transcript.get("clips") or []
    purity_rows = purity.get("clips") or []
    if not candidates or len(transcript_rows) != len(candidates) or len(purity_rows) != len(candidates):
        raise RuntimeError("The downstream smoke produced no clips or inconsistent Transcript QC.")
    candidate_ids = {str(row.get("id")) for row in candidates}
    if {str(row.get("clip_id")) for row in transcript_rows} != candidate_ids \
            or {str(row.get("clip_id")) for row in purity_rows} != candidate_ids:
        raise RuntimeError("TC/SP rows do not refer to the exact candidate clip identities.")
    if review_summary.get("slicer") != "VR" or review_summary.get("slicer_geometry") != "O0_4" \
            or review_summary.get("candidate_review_clips") != len(candidates):
        raise RuntimeError("The output does not contain a consistent frozen VR O0_4 candidate review summary.")
    if transcript_summary.get("schema_version") != 1 or transcript_summary.get("scored_count") != len(candidates) \
            or purity_summary.get("schema_version") != 1 or purity_summary.get("scored_count") != len(candidates):
        raise RuntimeError("Transcript QC or Speaker Purity summary failed its schema/count checks.")
    if purity.get("embedding_backend") != "sherpa-onnx" or purity.get("score_calibration") != "pending_evaluation" \
            or any(row.get("speaker_score_calibrated") is not False for row in purity_rows):
        raise RuntimeError("Speaker Purity did not retain its experimental uncalibrated backend metadata.")
    tc_scores = [row.get("transcript_match_score") for row in transcript_rows]
    sp_scores = [row.get("speaker_check_score") for row in purity_rows]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in tc_scores + sp_scores):
        raise RuntimeError("The downstream smoke produced a missing or non-finite TC/SP score.")
    for row in candidates:
        output = run_root / row["audio_path"]
        if not output.is_file() or output.stat().st_size <= 44:
            raise RuntimeError(f"Candidate audio is missing or empty: {row.get('clip_id')}")
        info = sf.info(str(output))
        if info.samplerate != 16_000 or info.channels != 1 or info.duration < 3 or info.duration > 15:
            raise RuntimeError(f"Candidate audio does not match expected VR output properties: {row.get('clip_id')}")
    for row in transcript_rows:
        if not str(row.get("whisper_text") or "").strip():
            raise RuntimeError(f"Whisper produced an empty transcript for candidate {row.get('clip_id')}")
    candidate_by_id = {str(row.get("id")): row for row in candidates}
    for row in exports:
        output = run_root / row["audio_path"]
        if not output.is_file() or output.stat().st_size <= 44:
            raise RuntimeError(f"Native export audio is missing or empty: {row.get('id')}")
        candidate = candidate_by_id.get(str(row.get("candidate_id")))
        if not candidate or row.get("source_candidate_audio_path") != candidate.get("audio_path"):
            raise RuntimeError(f"Native export references an unknown or mismatched candidate: {row.get('id')}")
    result = {"schema_version": 1, "result": "downstream_pipeline_validated_with_reference_speaker_regions",
        "test_only_reference_regions": True, "production_diarization": False,
        "runtime_seconds": round(time.monotonic() - started, 3), "source": str(source),
        "source_duration_seconds": audio_info.duration, "source_sample_rate": audio_info.samplerate,
        "source_channels": audio_info.channels, "candidate_count": len(candidates),
        "transcript_qc": {"model": transcript.get("model"), "score_method": transcript.get("score_method"),
            "scored_count": len(tc_scores), "failed_count": 0,
            "score_min": min(tc_scores), "score_max": max(tc_scores)},
        "speaker_purity": {"model": purity.get("model"), "backend": purity.get("embedding_backend"),
            "calibration_status": purity.get("score_calibration"), "clip_count": len(sp_scores),
            "uncalibrated_rows": sum(row.get("speaker_score_calibrated") is False for row in purity_rows),
            "score_min": min(sp_scores), "score_max": max(sp_scores)},
        "export_count": len(exports), "export_manifest": str(artifacts / "export_manifest.json"),
        "run_root": str(run_root)}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
