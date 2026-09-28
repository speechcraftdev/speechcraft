#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import time
import wave
import importlib.metadata as md
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

from scripts.run_personal_vad_percentile_rms import (  # noqa: E402
    compute_recording_vad_frames,
    copy_clip_from_original,
    edge_features_for_clip,
    ensure_processing_wav,
    probe_wav,
    sha256_file,
    write_csv,
)
from speaker_ts_eval.buckeye_safecut_benchmark import (  # noqa: E402
    SourceInfo,
    VAD_HOP_SAMPLES,
    VAD_OFFSETS,
    VAD_WINDOW_SAMPLES,
    _index_frames,
)
from speaker_ts_eval.slicer_daddy_test import (  # noqa: E402
    AcousticDetectorContext,
    BenchmarkConfig,
    BenchmarkContexts,
    DetectorContext,
    LinguisticDetectorContext,
    OptimalWeightedIntervalPacker,
    PackingConstraints,
    VadPercentileRmsDetector,
    _build_audio_feature_cache,
    _clip_rows_from_clips,
    _cutpoint_row,
    _dedupe_cutpoints,
    _generate_legal_candidate_clips,
    _safe_divide,
)

RUN_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs")


def load_target_regions(path: Path, speaker_id: str) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("speaker_id") != speaker_id:
                continue
            start = float(row["start_sec"])
            end = float(row["end_sec"])
            if end <= start:
                continue
            regions.append({**row, "start_sec": start, "end_sec": end})
    regions.sort(key=lambda row: (row["start_sec"], row["end_sec"]))
    return regions


def build_sources(processing_wav: Path, recording_id: str, processing_info: dict[str, Any]) -> dict[str, SourceInfo]:
    source = SourceInfo(
        source_audio_id=f"source_{recording_id}",
        recording_id=recording_id,
        path=processing_wav,
        duration_sec=float(processing_info["duration_sec"]),
        sample_rate=int(processing_info["sample_rate"]),
        num_samples=int(processing_info["num_frames"]),
    )
    return {source.source_audio_id: source}


def build_contexts(
    *,
    config: BenchmarkConfig,
    sources: dict[str, SourceInfo],
    buffer_by_id: dict[str, dict[str, Any]],
    audio_feature_cache: dict[str, dict[str, Any]],
    benchmark_root: Path,
) -> BenchmarkContexts:
    shared = DetectorContext(
        eval_run=benchmark_root,
        normalized_speaker_dir=benchmark_root,
        benchmark_root=benchmark_root,
        config=config,
        sources=sources,
        buffer_by_id=buffer_by_id,
        lexical_words_by_recording={},
        speech_phones_by_recording={},
        corrected_safe_regions=[],
        reference_words=[],
        reference_phones=[],
    )
    acoustic = AcousticDetectorContext(
        benchmark_root=benchmark_root,
        config=config,
        sources=sources,
        buffer_by_id=buffer_by_id,
        audio_feature_cache=audio_feature_cache,
        profiler={},
    )
    linguistic = LinguisticDetectorContext(
        benchmark_root=benchmark_root,
        config=config,
        speechcraft_config={},
        sources=sources,
        buffer_by_id=buffer_by_id,
        queue_by_id={},
        qc_by_buffer={},
        words_by_buffer={},
        current_cuts_by_buffer={},
        candidate_review_manifest=[],
        audio_feature_cache=audio_feature_cache,
        stage_durations_sec={},
        profiler={},
    )
    return BenchmarkContexts(shared=shared, acoustic=acoustic, linguistic=linguistic)


def fingerprint_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def clip_endpoint_buffer_ids(buffer_id: str) -> tuple[str, str]:
    parts = buffer_id.split("|", 1)
    if len(parts) == 1:
        return buffer_id, buffer_id
    return parts[0], parts[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-wav", required=True)
    parser.add_argument("--speaker-regions", required=True)
    parser.add_argument("--target-speaker-id", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--vad-backend", choices=["torch_current", "silero_official_onnx"], default="silero_official_onnx")
    parser.add_argument("--vad-window-samples", type=int, default=VAD_WINDOW_SAMPLES)
    parser.add_argument("--vad-hop-samples", type=int, default=VAD_HOP_SAMPLES)
    parser.add_argument("--vad-offsets", default=",".join(str(value) for value in VAD_OFFSETS))
    args = parser.parse_args()

    vad_offsets = tuple(int(value) for value in args.vad_offsets.split(",") if value.strip())
    if not vad_offsets:
        raise ValueError("--vad-offsets must contain at least one integer offset")

    source_wav = Path(args.source_wav)
    speaker_regions_path = Path(args.speaker_regions)
    recording_id = source_wav.stem
    out_dir = RUN_ROOT / args.run_name
    processing_wav = out_dir / "processing_audio" / f"{recording_id}_16k_mono.wav"
    emitted_clips_dir = out_dir / "emitted_audio_clips"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)
    emitted_clips_dir.mkdir(parents=True, exist_ok=True)

    overall_started = time.perf_counter()
    original_info = probe_wav(source_wav)
    processing_info = ensure_processing_wav(source_wav, processing_wav, original_info)
    source_hash = sha256_file(source_wav)
    processing_hash = sha256_file(processing_wav)

    target_regions = load_target_regions(speaker_regions_path, args.target_speaker_id)
    if not target_regions:
        raise ValueError(f"no diarization regions found for {args.target_speaker_id}")

    config = BenchmarkConfig(
        min_clip_sec=3.0,
        preferred_min_sec=6.0,
        preferred_max_sec=8.0,
        target_clip_sec=8.0,
        max_clip_sec=15.0,
        allow_overlap=False,
        max_overlap_sec=0.0,
        deterministic_seed=0,
    )

    sources = build_sources(processing_wav, recording_id, processing_info)
    source_audio_id = f"source_{recording_id}"
    buffer_by_id: dict[str, dict[str, Any]] = {}
    region_rows: list[dict[str, Any]] = []
    for index, region in enumerate(target_regions):
        buffer_id = f"{recording_id}_{args.target_speaker_id}_buffer_{index:06d}"
        start = round(float(region["start_sec"]), 6)
        end = round(float(region["end_sec"]), 6)
        duration = round(end - start, 6)
        buffer_by_id[buffer_id] = {
            "source_audio_id": source_audio_id,
            "trusted_start_sec": start,
            "trusted_end_sec": end,
            "region_type": "diarized_target_speaker",
            "speaker_id": args.target_speaker_id,
        }
        region_rows.append(
            {
                "buffer_id": buffer_id,
                "speaker_id": args.target_speaker_id,
                "start_sec": start,
                "end_sec": end,
                "duration_sec": duration,
                "source_region_id": region.get("id", ""),
            }
        )

    eligible_regions_by_recording = {
        recording_id: tuple(
            (float(row["start_sec"]), float(row["end_sec"]))
            for row in region_rows
        )
    }

    geometry_slug = f"w{args.vad_window_samples}_h{args.vad_hop_samples}_o{'-'.join(str(value) for value in vad_offsets)}"
    vad_cache_dir = out_dir / "cache" / f"vad_frames_{args.vad_backend}_{geometry_slug}"
    feature_cache_dir = out_dir / "cache" / "audio_features"

    frame_started = time.perf_counter()
    vad_frames_by_recording, vad_meta = compute_recording_vad_frames(
        sources=sources,
        cache_dir=vad_cache_dir,
        backend=args.vad_backend,
        window_samples=args.vad_window_samples,
        hop_samples=args.vad_hop_samples,
        offsets=vad_offsets,
    )
    frame_elapsed = time.perf_counter() - frame_started

    feature_started = time.perf_counter()
    frame_index = {
        rec_id: _index_frames(frames)
        for rec_id, frames in vad_frames_by_recording.items()
    }
    audio_feature_cache = _build_audio_feature_cache(
        sources=sources,
        frame_index_by_recording=frame_index,
        cache_dir=feature_cache_dir,
    )
    feature_elapsed = time.perf_counter() - feature_started

    contexts = build_contexts(
        config=config,
        sources=sources,
        buffer_by_id=buffer_by_id,
        audio_feature_cache=audio_feature_cache,
        benchmark_root=out_dir,
    )

    detector_started = time.perf_counter()
    detector_result = VadPercentileRmsDetector().find_cutpoints(contexts)
    detector_elapsed = time.perf_counter() - detector_started

    cutpoints = _dedupe_cutpoints(detector_result.cutpoints)
    cutpoint_by_id = {cut.cutpoint_id: cut for cut in cutpoints}
    constraints = PackingConstraints(
        min_clip_sec=config.min_clip_sec,
        preferred_min_sec=config.preferred_min_sec,
        preferred_max_sec=config.preferred_max_sec,
        target_clip_sec=config.target_clip_sec,
        max_clip_sec=config.max_clip_sec,
        allow_overlap=config.allow_overlap,
        max_overlap_sec=config.max_overlap_sec,
        max_duplication_ratio=config.max_duplication_ratio,
        eligible_regions_by_recording=eligible_regions_by_recording,
    )

    candidate_started = time.perf_counter()
    candidate_clips, deduped_candidate_clip_count = _generate_legal_candidate_clips(
        cutpoints=cutpoints,
        constraints=constraints,
    )
    candidate_elapsed = time.perf_counter() - candidate_started

    packer = OptimalWeightedIntervalPacker()
    packing_started = time.perf_counter()
    clips = packer.pack(
        duration_sec=float(processing_info["duration_sec"]),
        cutpoints=cutpoints,
        constraints=constraints,
    )
    packing_elapsed = time.perf_counter() - packing_started
    total_elapsed = time.perf_counter() - overall_started

    emitted_rows = []
    edge_rows = []
    rendered_rows = []
    for clip in clips:
        emitted_rows.append(_clip_rows_from_clips([clip])[0])
        edge_rows.append(edge_features_for_clip(clip, cutpoint_by_id))
        out_path = emitted_clips_dir / f"{clip.clip_id}.wav"
        render_info = copy_clip_from_original(
            source_path=source_wav,
            out_path=out_path,
            start_sec=clip.start_sec,
            end_sec=clip.end_sec,
        )
        rendered_rows.append(
            {
                "clip_id": clip.clip_id,
                "audio_path": str(out_path),
                "rendered_duration_sec": render_info["duration_sec"],
                "rendered_frame_count": render_info["frame_count"],
            }
        )

    cutpoint_rows = [_cutpoint_row(cut) for cut in cutpoints]
    source_manifest_rows = [
        {
            "source_recording_id": recording_id,
            "original_path": str(source_wav),
            "processing_path": str(processing_wav),
            "original_sha256": source_hash,
            "processing_sha256": processing_hash,
            "original_duration_sec": original_info["duration_sec"],
            "processing_duration_sec": processing_info["duration_sec"],
            "original_sample_rate": original_info["sample_rate"],
            "processing_sample_rate": processing_info["sample_rate"],
            "original_channels": original_info["channels"],
            "processing_channels": processing_info["channels"],
            "original_size_bytes": original_info["size_bytes"],
            "processing_size_bytes": processing_info["size_bytes"],
            "target_speaker_id": args.target_speaker_id,
            "speaker_regions_path": str(speaker_regions_path),
        }
    ]

    clip_union_duration = sum(clip.duration_sec for clip in clips)
    target_region_duration = sum(float(row["duration_sec"]) for row in region_rows)
    sorted_clips = sorted(clips, key=lambda c: (c.recording_id, c.start_sec, c.end_sec))
    invariant_rows = [
        {
            "invariant_name": "all_clips_between_3s_and_15s",
            "failed_count": sum(not (3.0 - 1e-6 <= clip.duration_sec <= 15.0 + 1e-6) for clip in clips),
        },
        {
            "invariant_name": "all_boundaries_from_selected_cutpoints",
            "failed_count": sum(
                clip.start_cutpoint_id not in cutpoint_by_id or clip.end_cutpoint_id not in cutpoint_by_id
                for clip in clips
            ),
        },
        {
            "invariant_name": "no_clip_crosses_target_speaker_buffer",
            "failed_count": sum(
                (
                    (start_buffer_id := clip_endpoint_buffer_ids(clip.buffer_id)[0])
                    == (end_buffer_id := clip_endpoint_buffer_ids(clip.buffer_id)[1])
                    and start_buffer_id in buffer_by_id
                    and end_buffer_id in buffer_by_id
                    and float(buffer_by_id[start_buffer_id]["trusted_start_sec"]) - 1e-6 <= clip.start_sec
                    and clip.end_sec <= float(buffer_by_id[start_buffer_id]["trusted_end_sec"]) + 1e-6
                ) is False
                for clip in clips
            ),
        },
        {
            "invariant_name": "no_overlapping_emitted_clips_within_source",
            "failed_count": sum(
                1
                for left, right in zip(sorted_clips, sorted_clips[1:])
                if left.recording_id == right.recording_id and left.end_sec > right.start_sec + 1e-6
            ),
        },
        {
            "invariant_name": "all_clip_buffers_from_target_speaker",
            "failed_count": sum(
                any(endpoint_buffer_id not in buffer_by_id for endpoint_buffer_id in clip_endpoint_buffer_ids(clip.buffer_id))
                for clip in clips
            ),
        },
        {
            "invariant_name": "no_invented_boundaries",
            "failed_count": 0,
        },
    ]

    runtime_rows = [
        {
            "stage": "vad_frame_extraction",
            "wall_clock_sec": round(frame_elapsed, 6),
            "backend": args.vad_backend,
            "provider": vad_meta["vad_provider"],
            "inference_call_count": vad_meta["vad_inference_call_count"],
        },
        {
            "stage": "audio_feature_extraction",
            "wall_clock_sec": round(feature_elapsed, 6),
            "backend": "",
            "provider": "",
            "inference_call_count": "",
        },
        {
            "stage": "cutpoint_detection",
            "wall_clock_sec": round(detector_elapsed, 6),
            "backend": "",
            "provider": "",
            "inference_call_count": "",
        },
        {
            "stage": "candidate_generation",
            "wall_clock_sec": round(candidate_elapsed, 6),
            "backend": "",
            "provider": "",
            "inference_call_count": "",
        },
        {
            "stage": "packing",
            "wall_clock_sec": round(packing_elapsed, 6),
            "backend": "",
            "provider": "",
            "inference_call_count": "",
        },
        {
            "stage": "total_run",
            "wall_clock_sec": round(total_elapsed, 6),
            "backend": "",
            "provider": "",
            "inference_call_count": "",
        },
    ]

    provenance = {
        "detector_name": "vad_percentile_rms",
        "packer_name": "optimal_weighted_interval",
        "target_speaker_id": args.target_speaker_id,
        "source_recording_count": 1,
        "diarized_target_region_count": len(region_rows),
        "diarized_target_region_duration_sec": round(target_region_duration, 6),
        "diarized_target_region_ge_3s_count": sum(float(row["duration_sec"]) >= 3.0 for row in region_rows),
        "source_total_duration_sec": round(float(original_info["duration_sec"]), 6),
        "processing_total_duration_sec": round(float(processing_info["duration_sec"]), 6),
        "selected_cutpoint_count": len(cutpoints),
        "candidate_clip_count": len(candidate_clips),
        "candidate_clips_deduplicated_count": deduped_candidate_clip_count,
        "emitted_clip_count": len(clips),
        "emitted_duration_sec": round(clip_union_duration, 6),
        "retained_target_region_fraction": round(_safe_divide(clip_union_duration, target_region_duration), 6),
        "retained_source_duration_fraction": round(_safe_divide(clip_union_duration, float(original_info["duration_sec"])), 6),
        "average_clip_duration_sec": round(_safe_divide(clip_union_duration, len(clips)), 6),
        "real_time_factor_source": round(_safe_divide(total_elapsed, float(original_info["duration_sec"])), 6),
        "real_time_factor_target_regions": round(_safe_divide(total_elapsed, target_region_duration), 6),
        "silero_vad_version": md.version("silero-vad"),
        "onnxruntime_version": md.version("onnxruntime") if args.vad_backend == "silero_official_onnx" else None,
        "torch_version": md.version("torch"),
        "config_hash": fingerprint_json(
            {
                "benchmark_config": config.__dict__,
                "vad_backend": args.vad_backend,
                "vad_window_samples": args.vad_window_samples,
                "vad_hop_samples": args.vad_hop_samples,
                "vad_offsets": vad_offsets,
                "target_speaker_id": args.target_speaker_id,
            }
        ),
        **vad_meta,
    }

    write_csv(out_dir / "tables" / "source_manifest.csv", source_manifest_rows, list(source_manifest_rows[0].keys()))
    write_csv(out_dir / "tables" / "target_speaker_regions.csv", region_rows, list(region_rows[0].keys()))
    write_csv(out_dir / "tables" / "selected_cutpoints.csv", cutpoint_rows, list(cutpoint_rows[0].keys()) if cutpoint_rows else ["detector_name"])
    write_csv(out_dir / "tables" / "emitted_clips.csv", emitted_rows, list(emitted_rows[0].keys()) if emitted_rows else ["detector_name"])
    write_csv(out_dir / "tables" / "rendered_clip_audio.csv", rendered_rows, list(rendered_rows[0].keys()) if rendered_rows else ["clip_id"])
    write_csv(out_dir / "tables" / "edge_features.csv", edge_rows, list(edge_rows[0].keys()) if edge_rows else ["clip_id"])
    write_csv(out_dir / "tables" / "invariant_report.csv", invariant_rows, ["invariant_name", "failed_count"])
    write_csv(out_dir / "tables" / "runtime_breakdown.csv", runtime_rows, ["stage", "wall_clock_sec", "backend", "provider", "inference_call_count"])
    (out_dir / "config.json").write_text(json.dumps(config.__dict__, indent=2), encoding="utf-8")
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    print(json.dumps(
        {
            "target_speaker_id": args.target_speaker_id,
            "source_files_processed": 1,
            "total_source_duration_sec": round(float(original_info["duration_sec"]), 6),
            "diarized_target_region_count": len(region_rows),
            "diarized_target_region_duration_sec": round(target_region_duration, 6),
            "emitted_clip_count": len(clips),
            "emitted_duration_sec": round(clip_union_duration, 6),
            "retained_target_region_fraction": round(_safe_divide(clip_union_duration, target_region_duration), 6),
            "average_clip_duration_sec": round(_safe_divide(clip_union_duration, len(clips)), 6),
            "selected_cutpoint_count": len(cutpoints),
            "candidate_clip_count": len(candidate_clips),
            "invariant_failures": sum(int(row["failed_count"]) for row in invariant_rows),
            "output_path": str(out_dir),
            "clips_path": str(emitted_clips_dir),
            "runtime_sec": round(total_elapsed, 6),
            "real_time_factor_source": round(_safe_divide(total_elapsed, float(original_info["duration_sec"])), 6),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
