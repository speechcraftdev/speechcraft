from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .io import read_json, read_jsonl, resolve_under_root, sha256_file, write_json


def sec_to_sample(seconds: float, sample_rate: int) -> int:
    return int(round(seconds * sample_rate))


def read_analysis_audio(path: Path) -> tuple[Any, int]:
    import numpy as np
    import soundfile as sf

    samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if not isinstance(samples, np.ndarray):
        samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1:
        raise ValueError(f"Processing buffer source must be mono: {path}")
    return samples.astype(np.float32, copy=False), int(sample_rate)


def write_pcm16_mono(path: Path, samples: Any, sample_rate: int) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), samples, sample_rate, subtype="PCM_16")


def vad_speech_intervals(vad_segments: list[dict[str, Any]], source_audio_id: str) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for segment in vad_segments:
        if str(segment.get("source_audio_id")) != source_audio_id:
            continue
        start = int(segment.get("analysis_start_sample", segment.get("start_sample", 0)))
        end = int(segment.get("analysis_end_sample", segment.get("end_sample", 0)))
        if end > start:
            intervals.append((start, end))
    return sorted(intervals)


def overlaps(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def has_non_target_intrusion(gap_start: int, gap_end: int, non_target_regions: list[dict[str, Any]]) -> bool:
    if gap_end <= gap_start:
        return False
    return any(overlaps(gap_start, gap_end, int(row["start_sample"]), int(row["end_sample"])) for row in non_target_regions)


def merge_target_regions(
    speaker_regions: list[dict[str, Any]],
    *,
    source_audio_id: str,
    target_speaker_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_rows = [
        row
        for row in speaker_regions
        if str(row.get("source_audio_id")) == source_audio_id and int(row.get("end_sample", 0)) > int(row.get("start_sample", 0))
    ]
    target_rows = sorted(
        [row for row in source_rows if str(row.get("speaker_id")) == target_speaker_id],
        key=lambda row: int(row["start_sample"]),
    )
    non_target_rows = [row for row in source_rows if str(row.get("speaker_id")) != target_speaker_id]

    merged: list[dict[str, Any]] = []
    clean_gap_merges = 0
    non_target_blocked_merges = 0
    for row in target_rows:
        start_sample = int(row["start_sample"])
        end_sample = int(row["end_sample"])
        if not merged:
            merged.append(
                {
                    "trusted_region_id": f"{source_audio_id}_trusted_region_{len(merged):06d}",
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "included_region_ids": [str(row["id"])],
                }
            )
            continue
        previous = merged[-1]
        if not has_non_target_intrusion(int(previous["end_sample"]), start_sample, non_target_rows):
            previous["end_sample"] = max(int(previous["end_sample"]), end_sample)
            previous["included_region_ids"].append(str(row["id"]))
            clean_gap_merges += 1
        else:
            non_target_blocked_merges += 1
            merged.append(
                {
                    "trusted_region_id": f"{source_audio_id}_trusted_region_{len(merged):06d}",
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "included_region_ids": [str(row["id"])],
                }
            )

    return merged, {
        "target_regions": len(target_rows),
        "non_target_regions": len(non_target_rows),
        "clean_gap_merges": clean_gap_merges,
        "non_target_blocked_merges": non_target_blocked_merges,
    }


def trusted_regions_for_single_speaker(
    speech_intervals: list[tuple[int, int]],
    audio_len: int,
    *,
    allow_no_vad_full_span_fallback: bool,
) -> list[dict[str, Any]]:
    if not speech_intervals:
        if not allow_no_vad_full_span_fallback:
            return []
        return [{"trusted_region_id": "trusted_region_000000", "start_sample": 0, "end_sample": audio_len}]
    return [
        {
            "trusted_region_id": "trusted_region_000000",
            "start_sample": max(0, speech_intervals[0][0]),
            "end_sample": min(audio_len, speech_intervals[-1][1]),
        }
    ]


def run_processing_buffers(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Build VR packing scopes from trusted speaker regions.

    These are not ASR/MFA chunks. VR slicer packs 3–15 s clips inside each
    trusted region on the 16 kHz analysis WAV.
    """
    audio_variants_manifest_path = resolve_under_root(run_root, "artifacts/audio_variants_manifest.json")
    vad_segments_path = resolve_under_root(run_root, "artifacts/vad_segments.jsonl")
    speaker_regions_path = resolve_under_root(run_root, "artifacts/speaker_regions.jsonl")
    speaker_selection_path = resolve_under_root(run_root, "artifacts/speaker_selection.json")
    variants = list(read_json(audio_variants_manifest_path).get("variants") or [])
    vad_segments = read_jsonl(vad_segments_path)
    speaker_regions = read_jsonl(speaker_regions_path)
    selection = read_json(speaker_selection_path)
    allow_no_vad_full_span_fallback = bool(config.get("allow_no_vad_full_span_fallback", False))
    mode = str(config.get("mode") or "single_speaker")
    target_speaker_id = str(selection.get("target_speaker_id") or "").strip()
    if mode == "diarization" and not bool(selection.get("selected")):
        raise ValueError("speaker_selection.json does not select a target speaker")
    if not target_speaker_id:
        raise ValueError("speaker_selection.json is missing target_speaker_id")
    rows: list[dict[str, Any]] = []
    skipped_sources: list[dict[str, Any]] = []
    trusted_region_totals = Counter()

    for variant in variants:
        source_audio_id = str(variant["source_audio_id"])
        analysis_path = resolve_under_root(run_root, str(variant["path"]))
        samples, sample_rate = read_analysis_audio(analysis_path)
        expected_sample_rate = int(config.get("analysis_sample_rate") or 16000)
        if sample_rate != expected_sample_rate:
            raise ValueError(
                f"Analysis sample-rate mismatch for {source_audio_id}: {sample_rate} != {expected_sample_rate}"
            )
        speech_intervals = vad_speech_intervals(vad_segments, source_audio_id)
        trusted_regions, trusted_summary = merge_target_regions(
            speaker_regions,
            source_audio_id=source_audio_id,
            target_speaker_id=target_speaker_id,
        )
        trusted_region_totals.update(trusted_summary)
        if not trusted_regions and mode == "single_speaker":
            trusted_regions = trusted_regions_for_single_speaker(
                speech_intervals,
                len(samples),
                allow_no_vad_full_span_fallback=allow_no_vad_full_span_fallback,
            )
        if not trusted_regions:
            skipped_sources.append(
                {
                    "source_audio_id": source_audio_id,
                    "reason_codes": ["no_target_speaker_regions_detected" if mode == "diarization" else "no_speech_detected"],
                }
            )
            continue
        previous_end: int | None = None
        for region in trusted_regions:
            trusted_start = int(region["start_sample"])
            trusted_end = int(region["end_sample"])
            if previous_end is not None and trusted_start < previous_end:
                raise RuntimeError("trusted regions overlap")
            previous_end = trusted_end
            if trusted_end <= trusted_start:
                continue
            buffer_id = f"buffer_{len(rows):06d}"
            duration_samples = trusted_end - trusted_start
            rows.append(
                {
                    "buffer_id": buffer_id,
                    "source_audio_id": source_audio_id,
                    "analysis_audio_path": variant["path"],
                    "audio_path": variant["path"],
                    "source_start_sample": trusted_start,
                    "source_end_sample": trusted_end,
                    "source_start_sec": round(trusted_start / sample_rate, 6),
                    "source_end_sec": round(trusted_end / sample_rate, 6),
                    "trusted_start_sample": trusted_start,
                    "trusted_end_sample": trusted_end,
                    "trusted_start_sec": round(trusted_start / sample_rate, 6),
                    "trusted_end_sec": round(trusted_end / sample_rate, 6),
                    "trusted_local_start_sample": 0,
                    "trusted_local_end_sample": duration_samples,
                    "duration_samples": duration_samples,
                    "duration_sec": round(duration_samples / sample_rate, 6),
                    "sample_rate": sample_rate,
                    "split_strategy": "whole_region",
                    "reason_codes": [],
                    "left_provisional_boundary": False,
                    "right_provisional_boundary": False,
                    "target_speaker_id": target_speaker_id,
                    "trusted_region_id": region.get("trusted_region_id"),
                }
            )

    durations = sorted(float(row["duration_sec"]) for row in rows)
    buffers_path = resolve_under_root(run_root, "artifacts/processing_buffers.json")
    write_json(buffers_path, rows)
    summary = {
        "stage": "processing_buffers",
        "config_hash": str(config.get("config_hash") or ""),
        "input_artifact_hashes": {
            "audio_variants_manifest": sha256_file(audio_variants_manifest_path),
            "vad_segments_jsonl": sha256_file(vad_segments_path),
            "speaker_regions_jsonl": sha256_file(speaker_regions_path),
            "speaker_selection_json": sha256_file(speaker_selection_path),
        },
        "output_hashes": {
            "processing_buffers_json": sha256_file(buffers_path),
        },
        "buffer_count": len(rows),
        "skipped_source_count": len(skipped_sources),
        "skipped_sources": skipped_sources,
        "total_duration_sec": round(sum(durations), 6),
        "max_duration_sec": max(durations) if durations else 0.0,
        "min_duration_sec": min(durations) if durations else 0.0,
        "split_strategy_counts": {"whole_region": len(rows)} if rows else {},
        "reason_code_counts": {},
        "allow_no_vad_full_span_fallback": allow_no_vad_full_span_fallback,
        "mode": mode,
        "target_speaker_id": target_speaker_id,
        "trusted_region_summary": dict(trusted_region_totals),
        "slicer": "VR",
        "slicer_geometry": "O0_4",
    }
    write_json(resolve_under_root(run_root, "artifacts/processing_buffer_summary.json"), summary)
    return summary
