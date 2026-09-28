#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import shutil
import subprocess
import sys
import time
import wave
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")
if str(SPEAKER_TS_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(SPEAKER_TS_EVAL_SRC))

from speaker_ts_eval.buckeye_safecut_benchmark import (  # noqa: E402
    VAD_HOP_SAMPLES,
    VAD_OFFSETS,
    VAD_WINDOW_SAMPLES,
    SourceInfo,
    _index_frames,
)
from speaker_ts_eval.slicer_daddy_test import (  # noqa: E402
    EPSILON_SEC,
    AcousticDetectorContext,
    BenchmarkConfig,
    BenchmarkContexts,
    Clip,
    ComputeMetric,
    Cutpoint,
    DetectorContext,
    DetectorRunResult,
    LinguisticDetectorContext,
    OptimalWeightedIntervalPacker,
    PackingConstraints,
    VadPercentileRmsDetector,
    _build_audio_feature_cache,
    _candidate_score,
    _clip_rows_from_clips,
    _cutpoint_row,
    _dedupe_cutpoints,
    _generate_legal_candidate_clips,
    _safe_divide,
)

RUN_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs/vad_percentile_rms_postvad_optimize_2026-07-20")
REVIEW_ZIP = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs/vad_percentile_rms_postvad_optimize_review_bundle_2026-07-20.zip")
ONNX_REFERENCE_ROOT = Path("/home/aaravthegreat/Projects/speechcraft/eval_runs/silero_onnx_vad_compare_2026-07-20")
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


def load_onnx_vad_frames(recording_id: str) -> list[dict[str, Any]]:
    cache_path = ONNX_REFERENCE_ROOT / recording_id / "silero_official_onnx" / "cache" / "vad_frames" / f"{recording_id}.json"
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        return [
            {
                "window_start_sec": float(row["window_start_sec"]),
                "window_end_sec": float(row["window_end_sec"]),
                "center_sec": float(row["center_sec"]),
                "speech_prob": float(row["speech_prob"]),
                "offset_samples": int(row.get("offset_samples", 0)),
            }
            for row in rows
        ]
    path = ONNX_REFERENCE_ROOT / recording_id / "silero_official_onnx" / "tables" / "vad_frames.csv"
    rows = read_csv(path)
    frames = []
    for row in rows:
        frames.append(
            {
                "window_start_sec": float(row["timestamp_sec"]) - (VAD_WINDOW_SAMPLES / 2 / 16000.0),
                "window_end_sec": float(row["timestamp_sec"]) + (VAD_WINDOW_SAMPLES / 2 / 16000.0),
                "center_sec": float(row["timestamp_sec"]),
                "speech_prob": float(row["probability"]),
                "offset_samples": int(float(row.get("offset_samples", 0) or 0)),
            }
        )
    return frames


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


def percentile_value(values: Any, percentile: float) -> float:
    import numpy as np

    if len(values) == 0:
        raise ValueError("cannot compute percentile of an empty array")
    ordered = np.sort(values.astype(float))
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (percentile / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return float(ordered[int(rank)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def merge_selected_windows(starts: Any, ends: Any, selected_indices: Any) -> list[tuple[float, float, int, int]]:
    runs: list[tuple[float, float, int, int]] = []
    if len(selected_indices) == 0:
        return runs
    run_start = float(starts[selected_indices[0]])
    run_end = float(ends[selected_indices[0]])
    first_pos = 0
    last_pos = 0
    for pos, idx in enumerate(selected_indices[1:], start=1):
        start = float(starts[idx])
        end = float(ends[idx])
        if start <= run_end + EPSILON_SEC:
            if end > run_end:
                run_end = end
            last_pos = pos
        else:
            runs.append((run_start, run_end, first_pos, last_pos))
            run_start = start
            run_end = end
            first_pos = pos
            last_pos = pos
    runs.append((run_start, run_end, first_pos, last_pos))
    return runs


def longest_below_ms(starts: Any, ends: Any, indices: Any, probs: Any, threshold: float) -> float:
    chosen = [int(idx) for idx in indices if float(probs[idx]) < threshold]
    if not chosen:
        return 0.0
    longest = 0.0
    run_start = float(starts[chosen[0]])
    run_end = float(ends[chosen[0]])
    for idx in chosen[1:]:
        start = float(starts[idx])
        end = float(ends[idx])
        if start <= run_end + EPSILON_SEC:
            run_end = max(run_end, end)
        else:
            longest = max(longest, run_end - run_start)
            run_start = start
            run_end = end
    longest = max(longest, run_end - run_start)
    return longest * 1000.0


def make_cutpoint(
    *,
    detector_name: str,
    source: SourceInfo,
    buffer_id: str,
    region_index: int,
    run_index: int,
    region_start: float,
    region_end: float,
    run_start: float,
    run_end: float,
    run_indices: Any,
    centers: Any,
    starts: Any,
    ends: Any,
    probs: Any,
    rms_dbfs: Any,
    voicing: Any,
    use_vad: bool,
    use_rms: bool,
    use_voicing: bool,
) -> Cutpoint:
    run_center = (run_start + run_end) / 2.0
    best_idx = min(
        (int(idx) for idx in run_indices),
        key=lambda idx: (
            float(probs[idx]),
            float(rms_dbfs[idx]),
            abs(float(centers[idx]) - run_center),
        ),
    )
    speech_values = probs[run_indices]
    rms_values = rms_dbfs[run_indices]
    voicing_values = voicing[run_indices]
    center = float(centers[best_idx])
    cut_id = f"{buffer_id}-{detector_name}-{region_index:04d}-{run_index:04d}-{int(round(center * 1000.0))}"
    return Cutpoint(
        detector_name=detector_name,
        source_id=source.source_audio_id,
        recording_id=source.recording_id,
        buffer_id=buffer_id,
        cutpoint_id=cut_id,
        time_sec=round(center, 6),
        interval_start_sec=round(run_start, 6),
        interval_end_sec=round(run_end, 6),
        score=round(
            _candidate_score(
                speech_prob=float(probs[best_idx]),
                rms_dbfs=float(rms_dbfs[best_idx]),
                voicing_score=float(voicing[best_idx]),
                use_vad=use_vad,
                use_rms=use_rms,
                use_voicing=use_voicing,
            ),
            6,
        ),
        vad_min=float(speech_values.min()),
        vad_mean=float(speech_values.mean()),
        vad_longest_below_0_1_ms=longest_below_ms(starts, ends, run_indices, probs, 0.1),
        vad_longest_below_0_2_ms=longest_below_ms(starts, ends, run_indices, probs, 0.2),
        rms_min_dbfs=float(rms_values.min()),
        rms_mean_dbfs=float(rms_values.mean()),
        voicing_min=float(voicing_values.min()),
        voicing_mean=float(voicing_values.mean()),
        compute_time_sec=0.0,
        is_buffer_edge_candidate=False,
        metadata={
            "region_index": region_index,
            "run_index": run_index,
            "run_duration_ms": round((run_end - run_start) * 1000.0, 6),
            "region_start_sec": round(region_start, 6),
            "region_end_sec": round(region_end, 6),
            "placement": "lowest_rms_frame",
            "optimized_postvad_path": True,
        },
    )


def build_audio_feature_cache_vectorized(
    *,
    sources: dict[str, SourceInfo],
    vad_frames_by_recording: dict[str, list[dict[str, Any]]],
    cache_dir: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    import numpy as np
    import soundfile as sf

    timings = Counter()
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, Any]] = {}
    for source in sources.values():
        t0 = time.perf_counter()
        samples, sample_rate = sf.read(str(source.path), dtype="float32", always_2d=False)
        audio = np.asarray(samples, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)
        timings["waveform_framing"] += time.perf_counter() - t0

        rows = vad_frames_by_recording[source.recording_id]
        starts_sec = np.asarray([float(row["window_start_sec"]) for row in rows], dtype=np.float64)
        ends_sec = np.asarray([float(row["window_end_sec"]) for row in rows], dtype=np.float64)
        starts = np.maximum(0, np.rint(starts_sec * sample_rate).astype(np.int64))
        ends = np.minimum(len(audio), np.rint(ends_sec * sample_rate).astype(np.int64))
        lengths = np.maximum(0, ends - starts)

        t1 = time.perf_counter()
        squared = audio.astype(np.float64) * audio.astype(np.float64)
        cumulative = np.concatenate(([0.0], np.cumsum(squared)))
        energy = cumulative[ends] - cumulative[starts]
        rms = np.zeros(len(rows), dtype=np.float64)
        valid = lengths > 0
        rms[valid] = np.sqrt(energy[valid] / lengths[valid])
        rms_dbfs = np.where(rms <= 1e-8, -120.0, 20.0 * np.log10(rms))
        timings["rms_computation"] += time.perf_counter() - t1

        t2 = time.perf_counter()
        signs = np.signbit(audio)
        sign_changes = (signs[1:] != signs[:-1]).astype(np.int32)
        change_cumulative = np.concatenate(([0], np.cumsum(sign_changes)))
        change_starts = np.minimum(starts, len(sign_changes))
        change_ends = np.minimum(np.maximum(starts, ends - 1), len(sign_changes))
        change_counts = change_cumulative[change_ends] - change_cumulative[change_starts]
        zcr = np.zeros(len(rows), dtype=np.float64)
        zcr_valid = lengths > 1
        zcr[zcr_valid] = change_counts[zcr_valid] / (lengths[zcr_valid] - 1)
        voicing = np.maximum(0.0, np.minimum(1.0, 1.0 - np.minimum(zcr / 0.5, 1.0)))
        timings["local_minimum_prominence_computation"] += time.perf_counter() - t2

        t3 = time.perf_counter()
        out_rows = []
        for row, rms_value, voice_value in zip(rows, rms_dbfs, voicing):
            out_rows.append(
                {
                    "window_start_sec": round(float(row["window_start_sec"]), 6),
                    "window_end_sec": round(float(row["window_end_sec"]), 6),
                    "center_sec": round(float(row["center_sec"]), 6),
                    "speech_prob": float(row["speech_prob"]),
                    "rms_dbfs": round(float(rms_value), 6),
                    "voicing_score": round(float(voice_value), 6),
                    "offset_samples": int(row.get("offset_samples", 0)),
                }
            )
        timings["candidate_construction"] += time.perf_counter() - t3
        payload = {"frames": out_rows, "centers": [float(row["center_sec"]) for row in out_rows]}
        (cache_dir / f"{source.recording_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        results[source.recording_id] = payload
    return results, {key: round(value, 6) for key, value in timings.items()}


class OptimizedVadPercentileRmsDetector:
    detector_name = "vad_percentile_rms"
    use_vad = True
    use_rms = True
    use_voicing = False
    include_edges = False

    def find_cutpoints(self, contexts: BenchmarkContexts) -> tuple[DetectorRunResult, dict[str, float]]:
        import numpy as np

        context = contexts.acoustic
        config = context.config
        cuts: list[Cutpoint] = []
        searched_buffer_ids: set[str] = set()
        timings = Counter()
        started = time.perf_counter()
        for buffer_id, buffer in sorted(context.buffer_by_id.items()):
            source_id = str(buffer["source_audio_id"])
            source = context.sources[source_id]
            payload = context.audio_feature_cache[source.recording_id]
            frames = payload["frames"]
            t0 = time.perf_counter()
            centers = np.asarray([float(row["center_sec"]) for row in frames], dtype=np.float64)
            starts = np.asarray([float(row["window_start_sec"]) for row in frames], dtype=np.float64)
            ends = np.asarray([float(row["window_end_sec"]) for row in frames], dtype=np.float64)
            probs = np.asarray([float(row["speech_prob"]) for row in frames], dtype=np.float64)
            rms_dbfs = np.asarray([float(row["rms_dbfs"]) for row in frames], dtype=np.float64)
            voicing = np.asarray([float(row["voicing_score"]) for row in frames], dtype=np.float64)
            region_start = float(buffer["trusted_start_sec"])
            region_end = float(buffer["trusted_end_sec"])
            region_mask = (centers > region_start + EPSILON_SEC) & (centers < region_end - EPSILON_SEC)
            region_indices = np.flatnonzero(region_mask)
            timings["waveform_framing"] += time.perf_counter() - t0
            if len(region_indices) == 0:
                continue
            searched_buffer_ids.add(buffer_id)
            region_index = 0

            t1 = time.perf_counter()
            vad_mask = probs[region_indices] < float(config.vad_threshold)
            timings["vad_rms_mask_combination"] += time.perf_counter() - t1
            if not vad_mask.any():
                continue

            t2 = time.perf_counter()
            threshold = percentile_value(rms_dbfs[region_indices], float(config.percentile_rms_percentile)) + float(config.percentile_rms_margin_db)
            timings["percentile_threshold_computation"] += time.perf_counter() - t2

            t3 = time.perf_counter()
            selected_region_indices = region_indices[vad_mask & (rms_dbfs[region_indices] <= threshold)]
            timings["vad_rms_mask_combination"] += time.perf_counter() - t3
            if len(selected_region_indices) == 0:
                continue

            t4 = time.perf_counter()
            runs = merge_selected_windows(starts, ends, selected_region_indices)
            timings["quiet_context_computation"] += time.perf_counter() - t4
            for run_index, (run_start, run_end, first_pos, last_pos) in enumerate(runs):
                if (run_end - run_start) * 1000.0 < float(config.vad_min_run_ms):
                    continue
                run_indices = selected_region_indices[first_pos : last_pos + 1]
                t5 = time.perf_counter()
                cuts.append(
                    make_cutpoint(
                        detector_name=self.detector_name,
                        source=source,
                        buffer_id=buffer_id,
                        region_index=region_index,
                        run_index=run_index,
                        region_start=region_start,
                        region_end=region_end,
                        run_start=run_start,
                        run_end=run_end,
                        run_indices=run_indices,
                        centers=centers,
                        starts=starts,
                        ends=ends,
                        probs=probs,
                        rms_dbfs=rms_dbfs,
                        voicing=voicing,
                        use_vad=self.use_vad,
                        use_rms=self.use_rms,
                        use_voicing=self.use_voicing,
                    )
                )
                timings["candidate_construction"] += time.perf_counter() - t5
        t6 = time.perf_counter()
        deduped = _dedupe_cutpoints(cuts)
        timings["candidate_deduplication"] += time.perf_counter() - t6
        metric = ComputeMetric(
            detector_name=self.detector_name,
            wall_clock_sec=round(time.perf_counter() - started, 6),
            cpu_time_sec=round(time.process_time(), 6),
            gpu_time_sec=None,
            peak_rss_mb=None,
            peak_gpu_memory_mb=None,
            real_time_factor=None,
            model_init_time_sec=None,
            per_hour_audio_cost_sec=None,
            external_model_dependencies=(),
            cold_audio_decode_sec=None,
            cold_rms_extraction_sec=None,
            cold_vad_model_load_sec=None,
            cold_vad_inference_sec=None,
            cold_voicing_extraction_sec=None,
            cold_candidate_generation_sec=None,
            cold_packing_sec=None,
            warm_audio_decode_sec=None,
            warm_rms_extraction_sec=None,
            warm_vad_model_load_sec=None,
            warm_vad_inference_sec=None,
            warm_voicing_extraction_sec=None,
            warm_candidate_generation_sec=None,
            warm_packing_sec=None,
        )
        return (
            DetectorRunResult(
                cutpoints=deduped,
                compute_metric=metric,
                searched_buffer_ids=frozenset(searched_buffer_ids),
                searched_duration_sec=0.0,
                searched_interval_count=len(searched_buffer_ids),
                uses_asr=False,
                uses_mfa=False,
                uses_aligned_words=False,
                uses_alignment_qc=False,
                validity_notes="Optimized array path for vad_percentile_rms equivalence test.",
            ),
            {key: round(value, 6) for key, value in timings.items()},
        )


def run_pipeline(
    *,
    mode: str,
    source: SourceInfo,
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
    mode_dir = out_dir / mode
    (mode_dir / "tables").mkdir(parents=True, exist_ok=True)
    vad_frames = {source.recording_id: load_onnx_vad_frames(source.recording_id)}

    feature_started = time.perf_counter()
    if mode in {"reference_onnx_postvad", "optimized_postvad"}:
        frame_index = {recording_id: _index_frames(frames) for recording_id, frames in vad_frames.items()}
        audio_features = _build_audio_feature_cache(
            sources=sources,
            frame_index_by_recording=frame_index,
            cache_dir=mode_dir / "cache" / "audio_features",
        )
        feature_subtimings = {
            "waveform_framing": 0.0,
            "rms_computation": 0.0,
            "local_minimum_prominence_computation": 0.0,
            "candidate_construction": 0.0,
        }
    elif mode == "optimized_vectorized_features_probe":
        audio_features, feature_subtimings = build_audio_feature_cache_vectorized(
            sources=sources,
            vad_frames_by_recording=vad_frames,
            cache_dir=mode_dir / "cache" / "audio_features",
        )
    else:
        raise ValueError(mode)
    feature_sec = time.perf_counter() - feature_started

    contexts = build_contexts(
        config=config,
        sources=sources,
        buffer_by_id=buffer_by_id,
        audio_feature_cache=audio_features,
        benchmark_root=mode_dir,
    )

    detect_started = time.perf_counter()
    if mode == "reference_onnx_postvad":
        result = VadPercentileRmsDetector().find_cutpoints(contexts)
        detect_subtimings = {
            "percentile_threshold_computation": 0.0,
            "local_minimum_prominence_computation": 0.0,
            "quiet_context_computation": 0.0,
            "vad_rms_mask_combination": 0.0,
            "candidate_construction": 0.0,
            "candidate_deduplication": 0.0,
            "packing_object_conversion": 0.0,
        }
    else:
        result, detect_subtimings = OptimizedVadPercentileRmsDetector().find_cutpoints(contexts)
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

    cutpoint_rows = [_cutpoint_row(cut) for cut in cutpoints]
    clip_rows = _clip_rows_from_clips(clips)
    write_csv(mode_dir / "tables" / "selected_cutpoints.csv", cutpoint_rows, list(cutpoint_rows[0].keys()) if cutpoint_rows else ["detector_name"])
    write_csv(mode_dir / "tables" / "emitted_clips.csv", clip_rows, list(clip_rows[0].keys()) if clip_rows else ["detector_name"])
    runtime_rows = [
        {"stage": "audio_feature_extraction", "wall_clock_sec": round(feature_sec, 6)},
        {"stage": "cutpoint_detection", "wall_clock_sec": round(detect_sec, 6)},
        {"stage": "candidate_generation", "wall_clock_sec": round(candidate_sec, 6)},
        {"stage": "packing", "wall_clock_sec": round(pack_sec, 6)},
    ]
    for name, value in {**feature_subtimings, **detect_subtimings}.items():
        runtime_rows.append({"stage": f"substage_{name}", "wall_clock_sec": round(float(value), 6)})
    write_csv(mode_dir / "tables" / "runtime_breakdown.csv", runtime_rows, ["stage", "wall_clock_sec"])
    emitted_duration = sum(float(row["duration_sec"]) for row in clip_rows)
    summary = {
        "mode": mode,
        "recording_id": source.recording_id,
        "audio_feature_extraction_sec": round(feature_sec, 6),
        "cutpoint_detection_sec": round(detect_sec, 6),
        "candidate_generation_sec": round(candidate_sec, 6),
        "packing_sec": round(pack_sec, 6),
        "selected_cutpoint_count": len(cutpoints),
        "candidate_clip_count": len(candidates),
        "candidate_clips_deduplicated_count": deduped_candidates,
        "emitted_clip_count": len(clip_rows),
        "emitted_duration_sec": round(emitted_duration, 6),
        "retained_duration_fraction": round(_safe_divide(emitted_duration, float(source.duration_sec)), 6),
        "invariant_failures": sum(not (3.0 - 1e-6 <= clip.duration_sec <= 15.0 + 1e-6) for clip in clips),
    }
    (mode_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def compare_outputs(out_dir: Path) -> dict[str, Any]:
    reference_cuts = read_csv(out_dir / "reference_onnx_postvad" / "tables" / "selected_cutpoints.csv")
    optimized_cuts = read_csv(out_dir / "optimized_postvad" / "tables" / "selected_cutpoints.csv")
    reference_clips = read_csv(out_dir / "reference_onnx_postvad" / "tables" / "emitted_clips.csv")
    optimized_clips = read_csv(out_dir / "optimized_postvad" / "tables" / "emitted_clips.csv")
    reference_cut_times = [int(round(float(row["time_sec"]) * 1000.0)) for row in reference_cuts]
    optimized_cut_times = [int(round(float(row["time_sec"]) * 1000.0)) for row in optimized_cuts]
    reference_selected_cut_times = sorted(
        {int(round(float(row["start_sec"]) * 1000.0)) for row in reference_clips}
        | {int(round(float(row["end_sec"]) * 1000.0)) for row in reference_clips}
    )
    optimized_selected_cut_times = sorted(
        {int(round(float(row["start_sec"]) * 1000.0)) for row in optimized_clips}
        | {int(round(float(row["end_sec"]) * 1000.0)) for row in optimized_clips}
    )
    reference_clip_keys = sorted((int(round(float(row["start_sec"]) * 1000.0)), int(round(float(row["end_sec"]) * 1000.0))) for row in reference_clips)
    optimized_clip_keys = sorted((int(round(float(row["start_sec"]) * 1000.0)), int(round(float(row["end_sec"]) * 1000.0))) for row in optimized_clips)
    retained_reference = sum(float(row["duration_sec"]) for row in reference_clips)
    retained_optimized = sum(float(row["duration_sec"]) for row in optimized_clips)
    changed_candidate_times = sorted(set(reference_cut_times) ^ set(optimized_cut_times))
    changed_selected_times = sorted(set(reference_selected_cut_times) ^ set(optimized_selected_cut_times))
    changed_clip_keys = sorted(set(reference_clip_keys) ^ set(optimized_clip_keys))
    diff_rows = [
        {"diff_type": "candidate_timestamp_ms", "value": value}
        for value in changed_candidate_times[:1000]
    ] + [
        {"diff_type": "selected_cutpoint_timestamp_ms", "value": value}
        for value in changed_selected_times[:1000]
    ] + [
        {"diff_type": "emitted_clip_ms", "value": f"{start}-{end}"}
        for start, end in changed_clip_keys[:1000]
    ]
    write_csv(out_dir / "output_differences.csv", diff_rows, ["diff_type", "value"])
    return {
        "changed_candidate_cutpoint_count": len(changed_candidate_times),
        "changed_selected_cutpoint_count": len(changed_selected_times),
        "changed_emitted_clip_count": len(changed_clip_keys),
        "retained_duration_difference_sec": round(retained_optimized - retained_reference, 6),
    }


def package_bundle() -> None:
    if REVIEW_ZIP.exists():
        REVIEW_ZIP.unlink()
    with zipfile.ZipFile(REVIEW_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(RUN_ROOT.rglob("*")):
            if path.is_file():
                rel_to_run = path.relative_to(RUN_ROOT)
                if "processing_audio" in rel_to_run.parts or "cache" in rel_to_run.parts:
                    continue
                rel = path.relative_to(RUN_ROOT.parent)
                zf.write(path, rel)
        zf.write(Path(__file__), Path("code") / Path(__file__).name)
        compare_script = Path("/home/aaravthegreat/Projects/speechcraft/scripts/compare_silero_onnx_vad.py")
        if compare_script.exists():
            zf.write(compare_script, Path("code") / compare_script.name)


def main() -> None:
    import numpy as np

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
    source_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
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
        reference = run_pipeline(mode="reference_onnx_postvad", source=source, config=config, out_dir=source_dir)
        optimized = run_pipeline(mode="optimized_postvad", source=source, config=config, out_dir=source_dir)
        comparison = compare_outputs(source_dir)
        postvad_reference = reference["audio_feature_extraction_sec"] + reference["cutpoint_detection_sec"]
        postvad_optimized = optimized["audio_feature_extraction_sec"] + optimized["cutpoint_detection_sec"]
        comparison_rows.append(
            {
                "recording_id": recording_id,
                **comparison,
                "reference_audio_feature_extraction_sec": reference["audio_feature_extraction_sec"],
                "optimized_audio_feature_extraction_sec": optimized["audio_feature_extraction_sec"],
                "audio_feature_speedup": round(_safe_divide(reference["audio_feature_extraction_sec"], optimized["audio_feature_extraction_sec"]), 6),
                "reference_cutpoint_detection_sec": reference["cutpoint_detection_sec"],
                "optimized_cutpoint_detection_sec": optimized["cutpoint_detection_sec"],
                "cutpoint_detection_speedup": round(_safe_divide(reference["cutpoint_detection_sec"], optimized["cutpoint_detection_sec"]), 6),
                "reference_postvad_sec": round(postvad_reference, 6),
                "optimized_postvad_sec": round(postvad_optimized, 6),
                "postvad_speedup": round(_safe_divide(postvad_reference, postvad_optimized), 6),
                "reference_invariant_failures": reference["invariant_failures"],
                "optimized_invariant_failures": optimized["invariant_failures"],
            }
        )
        summary_rows.extend([reference, optimized])
    write_csv(RUN_ROOT / "source_manifest.csv", source_rows, list(source_rows[0].keys()))
    write_csv(RUN_ROOT / "postvad_runtime_summary.csv", summary_rows, list(summary_rows[0].keys()))
    write_csv(RUN_ROOT / "postvad_equivalence_and_speedup.csv", comparison_rows, list(comparison_rows[0].keys()))
    provenance = {
        "experiment": "behavior-preserving post-VAD profiling and optimization for vad_percentile_rms",
        "reference_backend": "official Silero ONNX CPU VAD probabilities from previous comparison run",
        "reference_root": str(ONNX_REFERENCE_ROOT),
        "optimization_scope": "audio feature extraction and cutpoint detection only",
        "thresholds_changed": False,
        "packing_changed": False,
        "python": sys.version,
        "platform": platform.platform(),
        "numpy_version": np.__version__,
        "silero_vad_version": "not imported; reused frozen ONNX frame probabilities from reference run",
        "config_hash": hashlib.sha256(json.dumps(config.__dict__, sort_keys=True).encode()).hexdigest(),
    }
    (RUN_ROOT / "config.json").write_text(json.dumps(config.__dict__, indent=2), encoding="utf-8")
    (RUN_ROOT / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    (RUN_ROOT / "README.md").write_text(
        "Post-VAD vad_percentile_rms profiling and behavior-preserving optimization. "
        "Reference and optimized paths use the same frozen official ONNX VAD frame probabilities, "
        "same thresholds, same candidate semantics, and same packer.\n",
        encoding="utf-8",
    )
    package_bundle()
    print(json.dumps({"run_root": str(RUN_ROOT), "review_zip": str(REVIEW_ZIP), "comparisons": comparison_rows}, indent=2))


if __name__ == "__main__":
    main()
