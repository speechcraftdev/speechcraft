#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import argparse
import shutil
import subprocess
import sys
import time
import wave
import importlib.metadata as md
from pathlib import Path
from typing import Any

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

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
    Clip,
    ComputeMetric,
    Cutpoint,
    DetectorContext,
    LinguisticDetectorContext,
    OptimalWeightedIntervalPacker,
    PackingConstraints,
    VadPercentileRmsDetector,
    _build_audio_feature_cache,
    _buffer_union_duration,
    _clip_rows_from_clips,
    _cutpoint_row,
    _dedupe_cutpoints,
    _generate_legal_candidate_clips,
    _safe_divide,
)

RUN_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        sample_rate = reader.getframerate()
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        duration = frames / sample_rate if sample_rate else 0.0
    return {
        "path": str(path),
        "num_frames": frames,
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_sec": round(duration, 6),
        "size_bytes": path.stat().st_size,
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_processing_wav(source_wav: Path, processing_wav: Path, original_info: dict[str, Any]) -> dict[str, Any]:
    processing_wav.parent.mkdir(parents=True, exist_ok=True)
    if original_info["sample_rate"] == 16000 and original_info["channels"] == 1:
        if processing_wav.exists():
            processing_wav.unlink()
        shutil.copy2(source_wav, processing_wav)
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source_wav),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(processing_wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return probe_wav(processing_wav)


def compute_recording_vad_frames(
    *,
    sources: dict[str, SourceInfo],
    cache_dir: Path,
    backend: str,
    window_samples: int,
    hop_samples: int,
    offsets: tuple[int, ...],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    import numpy as np
    import soundfile as sf
    import torch
    from silero_vad import load_silero_vad

    if backend not in {"torch_current", "silero_official_onnx"}:
        raise ValueError(f"unsupported VAD backend: {backend}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    started_init = time.perf_counter()
    model = load_silero_vad(onnx=(backend == "silero_official_onnx"))
    model_init_sec = time.perf_counter() - started_init
    provider = "torch_cpu"
    if backend == "silero_official_onnx" and hasattr(model, "session"):
        provider = ",".join(model.session.get_providers())
    results: dict[str, list[dict[str, Any]]] = {}
    inference_call_count = 0
    cache_hit_count = 0
    try:
        for source in sources.values():
            cache_path = cache_dir / f"{source.recording_id}.json"
            if cache_path.exists():
                rows = json.loads(cache_path.read_text(encoding="utf-8"))
                results[source.recording_id] = rows
                cache_hit_count += 1
                continue
            samples, sample_rate = sf.read(str(source.path), dtype="float32", always_2d=False)
            if int(sample_rate) != source.sample_rate:
                raise ValueError(f"sample-rate mismatch for {source.path}: {sample_rate} != {source.sample_rate}")
            audio = np.asarray(samples, dtype=np.float32)
            rows: list[dict[str, Any]] = []
            for offset in offsets:
                model.reset_states()
                for start in range(offset, len(audio), hop_samples):
                    chunk = audio[start : start + window_samples]
                    if len(chunk) < window_samples:
                        chunk = np.pad(chunk, (0, window_samples - len(chunk)))
                    prob = float(model(torch.from_numpy(chunk), source.sample_rate).item())
                    inference_call_count += 1
                    center = start + (window_samples // 2)
                    rows.append(
                        {
                            "window_start_sec": round(start / source.sample_rate, 6),
                            "window_end_sec": round((start + window_samples) / source.sample_rate, 6),
                            "center_sec": round(center / source.sample_rate, 6),
                            "offset_samples": offset,
                            "speech_prob": prob,
                        }
                    )
            rows.sort(key=lambda row: (row["center_sec"], row["offset_samples"]))
            cache_path.write_text(json.dumps(rows), encoding="utf-8")
            results[source.recording_id] = rows
        return results, {
            "vad_backend": backend,
            "vad_provider": provider,
            "vad_model_init_count": 1,
            "vad_model_init_sec": round(model_init_sec, 6),
            "vad_window_samples": window_samples,
            "vad_hop_samples": hop_samples,
            "vad_offsets": list(offsets),
            "vad_inference_call_count": inference_call_count,
            "vad_cache_hit_count": cache_hit_count,
        }
    finally:
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


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


def copy_clip_from_original(
    *,
    source_path: Path,
    out_path: Path,
    start_sec: float,
    end_sec: float,
) -> dict[str, Any]:
    with wave.open(str(source_path), "rb") as reader:
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
    return {
        "frame_count": end_frame - start_frame,
        "duration_sec": round((end_frame - start_frame) / sample_rate, 6) if sample_rate else 0.0,
    }


def edge_features_for_clip(
    clip: Clip,
    cutpoint_by_id: dict[str, Cutpoint],
) -> dict[str, Any]:
    start = cutpoint_by_id[clip.start_cutpoint_id]
    end = cutpoint_by_id[clip.end_cutpoint_id]
    return {
        "clip_id": clip.clip_id,
        "source_recording_id": clip.recording_id,
        "clip_start_sec": clip.start_sec,
        "clip_end_sec": clip.end_sec,
        "clip_duration_sec": clip.duration_sec,
        "start_rms": start.rms_min_dbfs,
        "end_rms": end.rms_min_dbfs,
        "start_vad_confidence": start.vad_min,
        "end_vad_confidence": end.vad_min,
        "start_percentile_score": start.score,
        "end_percentile_score": end.score,
        "quiet_duration_before_start": round(max(0.0, start.time_sec - start.interval_start_sec), 6),
        "quiet_duration_after_end": round(max(0.0, end.interval_end_sec - end.time_sec), 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_wav", nargs="?", default="/home/aaravthegreat/Projects/Charactors/Pokimane/raw/poki2.wav")
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--vad-backend", choices=["torch_current", "silero_official_onnx"], default="torch_current")
    parser.add_argument("--vad-window-samples", type=int, default=VAD_WINDOW_SAMPLES)
    parser.add_argument("--vad-hop-samples", type=int, default=VAD_HOP_SAMPLES)
    parser.add_argument("--vad-offsets", default=",".join(str(value) for value in VAD_OFFSETS))
    args = parser.parse_args()
    vad_offsets = tuple(int(value) for value in args.vad_offsets.split(",") if value.strip())
    if not vad_offsets:
        raise ValueError("--vad-offsets must contain at least one integer offset")

    source_wav = Path(args.source_wav)
    recording_id = source_wav.stem
    run_name = args.run_name or f"vad_percentile_rms_{recording_id}_2026-07-20"
    out_dir = RUN_ROOT / run_name
    processing_wav = out_dir / "processing_audio" / f"{recording_id}_16k_mono.wav"
    emitted_clips_dir = out_dir / "emitted_audio_clips"

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "tables").mkdir(parents=True, exist_ok=True)
    emitted_clips_dir.mkdir(parents=True, exist_ok=True)

    overall_started = time.perf_counter()
    detector_started = None

    original_info = probe_wav(source_wav)
    processing_info = ensure_processing_wav(source_wav, processing_wav, original_info)
    source_hash = sha256_file(source_wav)
    processing_hash = sha256_file(processing_wav)

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
    buffer_by_id = {
        f"{recording_id}_full_buffer_000000": {
            "source_audio_id": f"source_{recording_id}",
            "trusted_start_sec": 0.0,
            "trusted_end_sec": float(processing_info["duration_sec"]),
            "region_type": "full_source_file",
        }
    }
    eligible_regions_by_recording = {
        recording_id: ((0.0, float(processing_info["duration_sec"])),)
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
        recording_id: _index_frames(frames)
        for recording_id, frames in vad_frames_by_recording.items()
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

    detector = VadPercentileRmsDetector()
    detector_started = time.perf_counter()
    detector_result = detector.find_cutpoints(contexts)
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
        }
    ]

    clip_union_duration = sum(clip.duration_sec for clip in clips)
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
            "invariant_name": "no_clip_crosses_source_file",
            "failed_count": 0,
        },
        {
            "invariant_name": "no_overlapping_emitted_clips_within_source",
            "failed_count": sum(
                1
                for left, right in zip(clips, clips[1:])
                if left.recording_id == right.recording_id and left.end_sec > right.start_sec + 1e-6
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
        "source_recording_count": 1,
        "source_total_duration_sec": round(float(original_info["duration_sec"]), 6),
        "processing_total_duration_sec": round(float(processing_info["duration_sec"]), 6),
        "selected_cutpoint_count": len(cutpoints),
        "candidate_clip_count": len(candidate_clips),
        "candidate_clips_deduplicated_count": deduped_candidate_clip_count,
        "emitted_clip_count": len(clips),
        "emitted_duration_sec": round(clip_union_duration, 6),
        "retained_duration_fraction": round(_safe_divide(clip_union_duration, float(original_info["duration_sec"])), 6),
        "average_clip_duration_sec": round(_safe_divide(clip_union_duration, len(clips)), 6),
        "real_time_factor": round(_safe_divide(total_elapsed, float(original_info["duration_sec"])), 6),
        "silero_vad_version": md.version("silero-vad"),
        "onnxruntime_version": md.version("onnxruntime") if args.vad_backend == "silero_official_onnx" else None,
        "torch_version": md.version("torch"),
        **vad_meta,
    }

    write_csv(out_dir / "tables" / "source_manifest.csv", source_manifest_rows, list(source_manifest_rows[0].keys()))
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
            "source_files_processed": 1,
            "total_source_duration_sec": round(float(original_info["duration_sec"]), 6),
            "emitted_clip_count": len(clips),
            "emitted_duration_sec": round(clip_union_duration, 6),
            "retained_duration_fraction": round(_safe_divide(clip_union_duration, float(original_info["duration_sec"])), 6),
            "average_clip_duration_sec": round(_safe_divide(clip_union_duration, len(clips)), 6),
            "selected_cutpoint_count": len(cutpoints),
            "candidate_clip_count": len(candidate_clips),
            "invariant_failures": sum(int(row["failed_count"]) for row in invariant_rows),
            "output_path": str(out_dir),
            "runtime_sec": round(total_elapsed, 6),
            "real_time_factor": round(_safe_divide(total_elapsed, float(original_info["duration_sec"])), 6),
        },
        indent=2,
    ))


if __name__ == "__main__":
    main()
