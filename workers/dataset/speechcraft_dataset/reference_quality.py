"""Deterministic Reference Quality ranking derived from saved QC artifacts."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from .io import read_json_value, read_jsonl, sha256_file, write_json

ARTIFACT = "artifacts/reference_quality.json"
METHOD = "speaker_representativeness_duration_position_transcript_v2"
WEIGHTS = {"representativeness": 0.75, "duration": 0.05, "position": 0.1, "transcript_confidence": 0.1}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _duration(row: dict[str, Any]) -> float | None:
    samples, rate = row.get("duration_samples"), row.get("sample_rate")
    if isinstance(samples, int) and not isinstance(samples, bool) and samples >= 0 and isinstance(rate, int) and rate > 0:
        return samples / rate
    value = _number(row.get("duration_sec"))
    return value if value is not None and value >= 0 else None


def duration_preference(duration: float) -> float:
    return min(1.0, max(0.0, 1.0 - 0.08 * max(0.0, 6.0 - duration) - 0.04 * max(0.0, duration - 9.0)))


def position_preference(
    candidate: dict[str, Any], regions: list[dict[str, Any]], target_speaker_id: str | None,
    region_rate_by_source: dict[str, int],
) -> tuple[float, bool]:
    source = candidate.get("source_audio_id")
    rate = candidate.get("sample_rate")
    start, end = candidate.get("source_start_sample"), candidate.get("source_end_sample")
    if (not isinstance(source, str) or not source or not isinstance(rate, int) or rate <= 0
            or not isinstance(start, int) or not isinstance(end, int) or end < start
            or not isinstance(target_speaker_id, str) or not target_speaker_id):
        return 0.5, False
    clip_start, clip_end = start / rate, end / rate
    relevant = []
    for region in regions:
        if region.get("source_audio_id") != source or region.get("speaker_id") != target_speaker_id:
            continue
        region_rate = region_rate_by_source.get(source)
        rs, re = region.get("start_sample"), region.get("end_sample")
        if region_rate is None or region_rate <= 0 or not isinstance(rs, int) or not isinstance(re, int) or re <= rs:
            continue
        relevant.append((rs / region_rate, re / region_rate))
    if not relevant:
        return 0.5, False
    contained = [(a, b) for a, b in relevant if clip_start >= a and clip_end <= b]
    if contained:
        margin = max(min(clip_start - a, b - clip_end) for a, b in contained)
        return min(1.0, max(0.0, margin / 2.0)), True
    # A span that overlaps target speech but crosses its boundary is known to be unsafe.
    if any(clip_start < b and clip_end > a for a, b in relevant):
        return 0.0, True
    # Reliable coordinates exist for this source and speaker; being outside every
    # target region is a known zero rather than an invented neutral location.
    return 0.0, True


def calculate_reference_quality(
    candidates: list[dict[str, Any]], speaker_rows: list[dict[str, Any]], regions: list[dict[str, Any]],
    transcript_rows: list[dict[str, Any]], target_speaker_id: str | None,
    region_rate_by_source: dict[str, int],
) -> list[dict[str, Any]]:
    speaker_by_id = {str(row.get("clip_id") or row.get("id")): row for row in speaker_rows}
    transcript_by_id = {str(row.get("clip_id") or row.get("id")): row for row in transcript_rows}
    output = []
    for candidate in candidates:
        clip_id = str(candidate.get("id") or candidate.get("clip_id") or "")
        speaker = speaker_by_id.get(clip_id, {})
        transcript = transcript_by_id.get(clip_id, {})
        mean = _number(speaker.get("mean_window_similarity"))
        p10 = _number(speaker.get("p10_window_similarity"))
        duration = _duration(candidate)
        tc_value = _number(transcript.get("transcript_match_score"))
        transcript_confidence = tc_value / 100.0 if tc_value is not None and 0.0 <= tc_value <= 100.0 else None
        representativeness = min(1.0, max(0.0, 0.7 * mean + 0.3 * p10)) if mean is not None and p10 is not None else None
        duration_score = duration_preference(duration) if duration is not None else None
        position_score, position_available = position_preference(candidate, regions, target_speaker_id, region_rate_by_source)
        score = (
            75.0 * representativeness + 5.0 * duration_score + 10.0 * position_score + 10.0 * transcript_confidence
            if representativeness is not None and duration_score is not None and transcript_confidence is not None
            else None
        )
        reasons = []
        if representativeness is None:
            reasons.append("missing_similarity_statistics")
        if duration_score is None:
            reasons.append("missing_duration")
        if transcript_confidence is None:
            reasons.append("missing_transcript_confidence")
        output.append({
            "clip_id": clip_id,
            "reference_quality_score": min(100.0, max(0.0, score)) if score is not None and math.isfinite(score) else None,
            "representativeness": representativeness,
            "duration_preference": duration_score,
            "position_preference": position_score,
            "transcript_confidence": transcript_confidence,
            "position_available": position_available,
            "reason_codes": reasons,
        })
    return output


def run_reference_quality(run_root: Path) -> dict[str, Any]:
    artifacts = run_root / "artifacts"
    manifest_path = artifacts / "candidate_review_manifest.json"
    speaker_path = artifacts / "speaker_purity.json"
    transcript_path = artifacts / "transcript_qc.json"
    regions_path = artifacts / "speaker_regions.jsonl"
    selection_path = artifacts / "speaker_selection.json"
    variants_path = artifacts / "audio_variants_manifest.json"
    candidates = read_json_value(manifest_path)
    speaker_payload = read_json_value(speaker_path)
    if not isinstance(candidates, list):
        raise ValueError("candidate_review_manifest.json must contain a list")
    speaker_rows = speaker_payload.get("clips") if isinstance(speaker_payload, dict) else speaker_payload
    if not isinstance(speaker_rows, list):
        raise ValueError("speaker_purity.json is missing clips[]")
    transcript_payload = read_json_value(transcript_path)
    transcript_rows = transcript_payload.get("clips") if isinstance(transcript_payload, dict) else transcript_payload
    if not isinstance(transcript_rows, list):
        raise ValueError("transcript_qc.json is missing clips[]")
    selection = read_json_value(selection_path)
    variants = read_json_value(variants_path).get("variants", [])
    rates = {str(v.get("source_audio_id")): int(v.get("analysis_sample_rate") or 0) for v in variants if isinstance(v, dict)}
    regions = read_jsonl(regions_path)
    clips = calculate_reference_quality(
        candidates, speaker_rows, regions, transcript_rows, selection.get("target_speaker_id"), rates,
    )
    payload = {
        "schema_version": 2,
        "score_method": METHOD,
        "weights": WEIGHTS,
        "candidate_manifest_sha256": sha256_file(manifest_path),
        "input_sha256": {
            "speaker_purity": sha256_file(speaker_path), "speaker_regions": sha256_file(regions_path),
            "speaker_selection": sha256_file(selection_path), "audio_variants_manifest": sha256_file(variants_path),
            "transcript_qc": sha256_file(transcript_path),
        },
        "clips": clips,
    }
    write_json(artifacts / "reference_quality.json", payload)
    return {"clip_count": len(clips), "scored_count": sum(row["reference_quality_score"] is not None for row in clips)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill Reference Quality from saved TC and Speaker Purity statistics")
    parser.add_argument("--run-root", required=True)
    args = parser.parse_args(argv)
    summary = run_reference_quality(Path(args.run_root).expanduser().resolve())
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
