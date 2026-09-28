#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
import wave
import zipfile
from pathlib import Path
from typing import Any

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

from speaker_ts_eval.buckeye_safecut_benchmark import (  # noqa: E402
    DEFAULT_VAD_THRESHOLD,
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
    DetectorContext,
    LinguisticDetectorContext,
    OptimalWeightedIntervalPacker,
    PackingConstraints,
    VadPercentileRmsDetector,
    _build_audio_feature_cache,
    _clip_rows_from_clips,
    _compute_recording_vad_frames,
    _cutpoint_row,
    _dedupe_cutpoints,
    _generate_legal_candidate_clips,
    _safe_divide,
)

RUN_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs/silero_onnx_vad_compare_2026-07-20")
REVIEW_ZIP = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs/silero_onnx_vad_compare_review_bundle_2026-07-20.zip")
SOURCES = [
    Path("/home/aaravthegreat/Projects/Charactors/Pokimane/raw/poki2.wav"),
    Path("/home/aaravthegreat/Projects/Charactors/MckennaGrace/raw/Mckenna1.wav"),
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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


def ensure_processing_wav(source_wav: Path, processing_wav: Path) -> dict[str, Any]:
    original_info = probe_wav(source_wav)
    processing_wav.parent.mkdir(parents=True, exist_ok=True)
    if original_info["sample_rate"] == 16000 and original_info["channels"] == 1:
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


def build_source(processing_wav: Path, recording_id: str, info: dict[str, Any]) -> SourceInfo:
    return SourceInfo(
        source_audio_id=f"source_{recording_id}",
        recording_id=recording_id,
        path=processing_wav,
        duration_sec=float(info["duration_sec"]),
        sample_rate=int(info["sample_rate"]),
        num_samples=int(info["num_frames"]),
    )


def compute_onnx_vad_frames(sources: dict[str, SourceInfo], cache_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    import numpy as np
    import soundfile as sf
    import torch
    from silero_vad import load_silero_vad

    started_init = time.perf_counter()
    model = load_silero_vad(onnx=True)
    init_sec = time.perf_counter() - started_init
    provider = ",".join(model.session.get_providers()) if hasattr(model, "session") else ""
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[dict[str, Any]]] = {}
    try:
        for source in sources.values():
            cache_path = cache_dir / f"{source.recording_id}.json"
            samples, sample_rate = sf.read(str(source.path), dtype="float32", always_2d=False)
            if int(sample_rate) != source.sample_rate:
                raise ValueError(f"sample-rate mismatch for {source.path}: {sample_rate} != {source.sample_rate}")
            audio = np.asarray(samples, dtype=np.float32)
            rows: list[dict[str, Any]] = []
            for offset in VAD_OFFSETS:
                model.reset_states()
                for start in range(offset, len(audio), VAD_HOP_SAMPLES):
                    chunk = audio[start : start + VAD_WINDOW_SAMPLES]
                    if len(chunk) < VAD_WINDOW_SAMPLES:
                        chunk = np.pad(chunk, (0, VAD_WINDOW_SAMPLES - len(chunk)))
                    prob = float(model(torch.from_numpy(chunk), source.sample_rate).item())
                    center = start + (VAD_WINDOW_SAMPLES // 2)
                    rows.append(
                        {
                            "window_start_sec": round(start / source.sample_rate, 6),
                            "window_end_sec": round((start + VAD_WINDOW_SAMPLES) / source.sample_rate, 6),
                            "center_sec": round(center / source.sample_rate, 6),
                            "offset_samples": offset,
                            "speech_prob": prob,
                        }
                    )
            rows.sort(key=lambda row: (row["center_sec"], row["offset_samples"]))
            cache_path.write_text(json.dumps(rows), encoding="utf-8")
            results[source.recording_id] = rows
        return results, {"model_init_sec": round(init_sec, 6), "provider": provider, "model_init_count": 1}
    finally:
        del model


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


def run_backend(
    *,
    backend_name: str,
    source: SourceInfo,
    source_wav: Path,
    config: BenchmarkConfig,
    out_dir: Path,
) -> dict[str, Any]:
    sources = {source.source_audio_id: source}
    buffer_by_id = {
        f"{source.recording_id}_full_buffer_000000": {
            "source_audio_id": source.source_audio_id,
            "trusted_start_sec": 0.0,
            "trusted_end_sec": float(source.duration_sec),
            "region_type": "full_source_file",
        }
    }
    backend_dir = out_dir / backend_name
    (backend_dir / "tables").mkdir(parents=True, exist_ok=True)

    total_started = time.perf_counter()
    frame_started = time.perf_counter()
    if backend_name == "torch_current":
        vad_frames = _compute_recording_vad_frames(sources, backend_dir / "cache" / "vad_frames")
        vad_meta = {"model_init_count": 1, "provider": "torch_cpu"}
    elif backend_name == "silero_official_onnx":
        vad_frames, vad_meta = compute_onnx_vad_frames(sources, backend_dir / "cache" / "vad_frames")
    else:
        raise ValueError(backend_name)
    vad_sec = time.perf_counter() - frame_started

    feature_started = time.perf_counter()
    frame_index = {recording_id: _index_frames(frames) for recording_id, frames in vad_frames.items()}
    audio_features = _build_audio_feature_cache(
        sources=sources,
        frame_index_by_recording=frame_index,
        cache_dir=backend_dir / "cache" / "audio_features",
    )
    feature_sec = time.perf_counter() - feature_started

    contexts = build_contexts(
        config=config,
        sources=sources,
        buffer_by_id=buffer_by_id,
        audio_feature_cache=audio_features,
        benchmark_root=backend_dir,
    )

    detect_started = time.perf_counter()
    result = VadPercentileRmsDetector().find_cutpoints(contexts)
    detect_sec = time.perf_counter() - detect_started
    cutpoints = _dedupe_cutpoints(result.cutpoints)
    constraints = PackingConstraints(
        min_clip_sec=config.min_clip_sec,
        preferred_min_sec=config.preferred_min_sec,
        preferred_max_sec=config.preferred_max_sec,
        target_clip_sec=config.target_clip_sec,
        max_clip_sec=config.max_clip_sec,
        allow_overlap=config.allow_overlap,
        max_overlap_sec=config.max_overlap_sec,
        max_duplication_ratio=config.max_duplication_ratio,
        eligible_regions_by_recording={source.recording_id: ((0.0, float(source.duration_sec)),)},
    )
    candidate_started = time.perf_counter()
    candidates, deduped_candidates = _generate_legal_candidate_clips(cutpoints=cutpoints, constraints=constraints)
    candidate_sec = time.perf_counter() - candidate_started

    pack_started = time.perf_counter()
    clips = OptimalWeightedIntervalPacker().pack(duration_sec=float(source.duration_sec), cutpoints=cutpoints, constraints=constraints)
    pack_sec = time.perf_counter() - pack_started
    total_sec = time.perf_counter() - total_started

    vad_rows = [
        {
            "source_id": source.source_audio_id,
            "frame_index": index,
            "timestamp_sec": row["center_sec"],
            "probability": row["speech_prob"],
            "threshold_decision": float(row["speech_prob"]) >= DEFAULT_VAD_THRESHOLD,
        }
        for index, row in enumerate(vad_frames[source.recording_id])
    ]
    cutpoint_rows = [_cutpoint_row(cut) for cut in cutpoints]
    clip_rows = _clip_rows_from_clips(clips)
    runtime_rows = [
        {"stage": "vad_frame_extraction", "wall_clock_sec": round(vad_sec, 6)},
        {"stage": "audio_feature_extraction", "wall_clock_sec": round(feature_sec, 6)},
        {"stage": "cutpoint_detection", "wall_clock_sec": round(detect_sec, 6)},
        {"stage": "candidate_generation", "wall_clock_sec": round(candidate_sec, 6)},
        {"stage": "packing", "wall_clock_sec": round(pack_sec, 6)},
        {"stage": "total_run", "wall_clock_sec": round(total_sec, 6)},
    ]
    write_csv(backend_dir / "tables" / "vad_frames.csv", vad_rows, list(vad_rows[0].keys()))
    write_csv(backend_dir / "tables" / "selected_cutpoints.csv", cutpoint_rows, list(cutpoint_rows[0].keys()) if cutpoint_rows else ["detector_name"])
    write_csv(backend_dir / "tables" / "emitted_clips.csv", clip_rows, list(clip_rows[0].keys()) if clip_rows else ["detector_name"])
    write_csv(backend_dir / "tables" / "runtime_breakdown.csv", runtime_rows, ["stage", "wall_clock_sec"])

    emitted_duration = sum(float(row["duration_sec"]) for row in clip_rows)
    invariant_failures = sum(
        not (3.0 - 1e-6 <= float(row["duration_sec"]) <= 15.0 + 1e-6)
        for row in clip_rows
    )
    summary = {
        "backend_name": backend_name,
        "recording_id": source.recording_id,
        "vad_frame_count": len(vad_rows),
        "selected_cutpoint_count": len(cutpoints),
        "candidate_clip_count": len(candidates),
        "candidate_clips_deduplicated_count": deduped_candidates,
        "emitted_clip_count": len(clip_rows),
        "emitted_duration_sec": round(emitted_duration, 6),
        "retained_duration_fraction": round(_safe_divide(emitted_duration, float(source.duration_sec)), 6),
        "invariant_failures": invariant_failures,
        "vad_runtime_sec": round(vad_sec, 6),
        "total_runtime_sec": round(total_sec, 6),
        "real_time_factor": round(_safe_divide(total_sec, float(source.duration_sec)), 6),
        **vad_meta,
    }
    (backend_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def compare_frames(out_dir: Path, source_id: str) -> dict[str, Any]:
    torch_rows = read_csv(out_dir / "torch_current" / "tables" / "vad_frames.csv")
    onnx_rows = read_csv(out_dir / "silero_official_onnx" / "tables" / "vad_frames.csv")
    if len(torch_rows) != len(onnx_rows):
        raise ValueError("VAD frame count mismatch")
    rows: list[dict[str, Any]] = []
    diffs: list[float] = []
    changed = 0
    for torch_row, onnx_row in zip(torch_rows, onnx_rows):
        if torch_row["timestamp_sec"] != onnx_row["timestamp_sec"]:
            raise ValueError("VAD frame timeline mismatch")
        torch_prob = float(torch_row["probability"])
        onnx_prob = float(onnx_row["probability"])
        diff = abs(torch_prob - onnx_prob)
        torch_decision = torch_prob >= DEFAULT_VAD_THRESHOLD
        onnx_decision = onnx_prob >= DEFAULT_VAD_THRESHOLD
        changed += int(torch_decision != onnx_decision)
        diffs.append(diff)
        rows.append(
            {
                "source_id": source_id,
                "frame_index": torch_row["frame_index"],
                "timestamp_sec": torch_row["timestamp_sec"],
                "torch_probability": torch_prob,
                "onnx_probability": onnx_prob,
                "absolute_difference": diff,
                "torch_threshold_decision": torch_decision,
                "onnx_threshold_decision": onnx_decision,
            }
        )
    write_csv(out_dir / "frame_probability_comparison.csv", rows, list(rows[0].keys()))
    ordered = sorted(diffs)
    def percentile(q: float) -> float:
        if not ordered:
            return 0.0
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
        return ordered[index]
    return {
        "frame_count": len(rows),
        "max_probability_difference": round(max(diffs), 9) if diffs else 0.0,
        "mean_probability_difference": round(sum(diffs) / len(diffs), 9) if diffs else 0.0,
        "p50_probability_difference": round(percentile(0.50), 9),
        "p95_probability_difference": round(percentile(0.95), 9),
        "p99_probability_difference": round(percentile(0.99), 9),
        "changed_threshold_decision_count": changed,
    }


def row_key(row: dict[str, str]) -> tuple[int, int]:
    return (int(round(float(row["start_sec"]) * 1000.0)), int(round(float(row["end_sec"]) * 1000.0)))


def compare_outputs(out_dir: Path, source_wav: Path) -> dict[str, Any]:
    torch_cuts = read_csv(out_dir / "torch_current" / "tables" / "selected_cutpoints.csv")
    onnx_cuts = read_csv(out_dir / "silero_official_onnx" / "tables" / "selected_cutpoints.csv")
    torch_clips = read_csv(out_dir / "torch_current" / "tables" / "emitted_clips.csv")
    onnx_clips = read_csv(out_dir / "silero_official_onnx" / "tables" / "emitted_clips.csv")
    torch_cut_times = {int(round(float(row["time_sec"]) * 1000.0)) for row in torch_cuts}
    onnx_cut_times = {int(round(float(row["time_sec"]) * 1000.0)) for row in onnx_cuts}
    torch_clip_keys = {row_key(row) for row in torch_clips}
    onnx_clip_keys = {row_key(row) for row in onnx_clips}
    torch_selected_cut_times = {
        int(round(float(row["start_sec"]) * 1000.0))
        for row in torch_clips
    } | {
        int(round(float(row["end_sec"]) * 1000.0))
        for row in torch_clips
    }
    onnx_selected_cut_times = {
        int(round(float(row["start_sec"]) * 1000.0))
        for row in onnx_clips
    } | {
        int(round(float(row["end_sec"]) * 1000.0))
        for row in onnx_clips
    }
    changed_clip_keys = sorted(torch_clip_keys ^ onnx_clip_keys)
    changed_rows: list[dict[str, Any]] = []
    changed_dir = out_dir / "changed_output_review_audio"
    changed_dir.mkdir(parents=True, exist_ok=True)
    clip_by_key = {("torch_current", row_key(row)): row for row in torch_clips}
    clip_by_key.update({("silero_official_onnx", row_key(row)): row for row in onnx_clips})
    for key in changed_clip_keys:
        for backend in ("torch_current", "silero_official_onnx"):
            row = clip_by_key.get((backend, key))
            if row is None:
                continue
            out_path = changed_dir / backend / f"{backend}_{row['start_sec']}_{row['end_sec']}.wav"
            copy_clip(source_wav, out_path, float(row["start_sec"]), float(row["end_sec"]))
            changed_rows.append({**row, "backend": backend, "review_audio_path": str(out_path)})
    if changed_rows:
        write_csv(out_dir / "changed_output_review_manifest.csv", changed_rows, list(changed_rows[0].keys()))
    else:
        write_csv(out_dir / "changed_output_review_manifest.csv", [], ["backend", "clip_id", "review_audio_path"])
    torch_duration = sum(float(row["duration_sec"]) for row in torch_clips)
    onnx_duration = sum(float(row["duration_sec"]) for row in onnx_clips)
    return {
        "changed_candidate_cutpoint_count": len(torch_cut_times ^ onnx_cut_times),
        "changed_selected_cutpoint_count": len(torch_selected_cut_times ^ onnx_selected_cut_times),
        "changed_emitted_clip_count": len(changed_clip_keys),
        "retained_duration_difference_sec": round(onnx_duration - torch_duration, 6),
    }


def copy_clip(source_path: Path, out_path: Path, start_sec: float, end_sec: float) -> None:
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


def package_bundle() -> None:
    if REVIEW_ZIP.exists():
        REVIEW_ZIP.unlink()
    with zipfile.ZipFile(REVIEW_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(RUN_ROOT.rglob("*")):
            if path.is_file():
                rel_to_run = path.relative_to(RUN_ROOT)
                if "processing_audio" in rel_to_run.parts or "cache" in rel_to_run.parts:
                    continue
                if path.name == "vad_frames.csv":
                    continue
                rel = path.relative_to(RUN_ROOT.parent)
                zf.write(path, rel)
        for path in [
            Path("/home/aaravthegreat/Projects/speechcraft/scripts/compare_silero_onnx_vad.py"),
            Path("/home/aaravthegreat/Projects/speechcraft/scripts/run_personal_vad_percentile_rms.py"),
        ]:
            zf.write(path, Path("code") / path.name)


def main() -> None:
    import importlib.metadata as md
    import importlib.resources as resources
    import numpy as np
    import onnxruntime

    if RUN_ROOT.exists():
        shutil.rmtree(RUN_ROOT)
    RUN_ROOT.mkdir(parents=True, exist_ok=True)

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
    config_hash = hashlib.sha256(json.dumps(config.__dict__, sort_keys=True).encode()).hexdigest()
    onnx_model_path = Path(str(resources.files("silero_vad.data").joinpath("silero_vad.onnx")))
    source_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for source_wav in SOURCES:
        recording_id = source_wav.stem
        source_dir = RUN_ROOT / recording_id
        processing_wav = source_dir / "processing_audio" / f"{recording_id}_16k_mono.wav"
        original_info = probe_wav(source_wav)
        processing_info = ensure_processing_wav(source_wav, processing_wav)
        source = build_source(processing_wav, recording_id, processing_info)
        source_rows.append(
            {
                "recording_id": recording_id,
                "source_wav_path": str(source_wav),
                "processing_wav_path": str(processing_wav),
                "source_sha256": sha256_file(source_wav),
                "processing_sha256": sha256_file(processing_wav),
                "source_duration_sec": original_info["duration_sec"],
                "processing_duration_sec": processing_info["duration_sec"],
                "source_sample_rate": original_info["sample_rate"],
                "processing_sample_rate": processing_info["sample_rate"],
            }
        )
        torch_summary = run_backend(
            backend_name="torch_current",
            source=source,
            source_wav=source_wav,
            config=config,
            out_dir=source_dir,
        )
        onnx_summary = run_backend(
            backend_name="silero_official_onnx",
            source=source,
            source_wav=source_wav,
            config=config,
            out_dir=source_dir,
        )
        frame_summary = compare_frames(source_dir, source.source_audio_id)
        output_summary = compare_outputs(source_dir, source_wav)
        speedup = _safe_divide(float(torch_summary["vad_runtime_sec"]), float(onnx_summary["vad_runtime_sec"]))
        total_speedup = _safe_divide(float(torch_summary["total_runtime_sec"]), float(onnx_summary["total_runtime_sec"]))
        comparison = {
            "recording_id": recording_id,
            **frame_summary,
            **output_summary,
            "torch_vad_runtime_sec": torch_summary["vad_runtime_sec"],
            "onnx_vad_runtime_sec": onnx_summary["vad_runtime_sec"],
            "vad_speedup": round(speedup, 6),
            "torch_total_runtime_sec": torch_summary["total_runtime_sec"],
            "onnx_total_runtime_sec": onnx_summary["total_runtime_sec"],
            "total_speedup": round(total_speedup, 6),
            "torch_real_time_factor": torch_summary["real_time_factor"],
            "onnx_real_time_factor": onnx_summary["real_time_factor"],
            "torch_invariant_failures": torch_summary["invariant_failures"],
            "onnx_invariant_failures": onnx_summary["invariant_failures"],
            "onnx_execution_provider": onnx_summary.get("provider", ""),
        }
        comparison_rows.append(comparison)
        summaries.extend([torch_summary, onnx_summary])

    provenance = {
        "silero_vad_version": md.version("silero-vad"),
        "onnxruntime_version": md.version("onnxruntime"),
        "numpy_version": np.__version__,
        "torch_version": md.version("torch"),
        "onnx_model_path": str(onnx_model_path),
        "onnx_model_sha256": sha256_file(onnx_model_path),
        "onnx_available_providers": onnxruntime.get_available_providers(),
        "provider": "CPUExecutionProvider",
        "sample_rate": 16000,
        "vad_window_samples": VAD_WINDOW_SAMPLES,
        "vad_hop_samples": VAD_HOP_SAMPLES,
        "vad_offsets": list(VAD_OFFSETS),
        "state_reset_behavior": "reset once per offset pass for each independent source file",
        "cpu": platform.processor(),
        "platform": platform.platform(),
        "configuration_hash": config_hash,
    }
    write_csv(RUN_ROOT / "source_manifest.csv", source_rows, list(source_rows[0].keys()))
    write_csv(RUN_ROOT / "backend_runtime_summary.csv", summaries, list(summaries[0].keys()))
    write_csv(RUN_ROOT / "comparison_summary.csv", comparison_rows, list(comparison_rows[0].keys()))
    (RUN_ROOT / "config.json").write_text(json.dumps(config.__dict__, indent=2), encoding="utf-8")
    (RUN_ROOT / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    (RUN_ROOT / "README.md").write_text(
        "Official Silero ONNX VAD comparison against the current Torch backend. "
        "Only VAD frame extraction differs; downstream detector and packer logic is unchanged.\n",
        encoding="utf-8",
    )
    package_bundle()
    print(json.dumps({"run_root": str(RUN_ROOT), "review_zip": str(REVIEW_ZIP), "comparisons": comparison_rows}, indent=2))


if __name__ == "__main__":
    main()
