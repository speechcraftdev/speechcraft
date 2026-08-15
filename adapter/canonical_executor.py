"""Canonical vad_percentile_rms executor via speaker_ts_eval (lab-side thin wrap).

Canonical research path (not reimplemented here):

    $SPEAKER_TS_EVAL_SRC (or sibling ../speaker_ts_eval/src)
      speaker_ts_eval/slicer_daddy_test.py          # VadPercentileRmsDetector + packer helpers
      speaker_ts_eval/repaired_buckeye_benchmark.py # per-buffer 3–15s packing
      speaker_ts_eval/buckeye_safecut_benchmark.py  # Silero VAD frame geometry

Speechcraft scripts (e.g. run_personal_vad_percentile_rms.py) call the same stack.

Every invocation uses a fresh workdir and fresh feature computation —
no shared acoustic cache or reusable detector context between geometries.
RMS features are computed on VAD-frame windows, so they are geometry-dependent
and must not be reused across different fingerprints. Optional in-memory reuse
goes through GeometryKeyedStore only.
Phase 3 still does not add a cross-geometry cache; A computes A, D computes D.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adapter.config import GeometryConfig
from adapter.contender_policy import apply_candidate_weight_policy, apply_cut_policy
from adapter.diagnostics import (
    ExecutionDiagnostics,
    SPEAKER_TS_EVAL_SRC_ENV,
    geometry_canonical_json,
    geometry_fingerprint,
    hash_clip_intervals,
    hash_cutpoint_times,
    hash_vad_probabilities,
    hash_vad_timestamps,
    policy_canonical_json,
    policy_fingerprint,
    resolve_speaker_ts_eval_src,
)
from adapter.types import RawClip, RawCutpoint, RawSlicerOutput, SlicerRequest

_VAD_ENV_KEYS = (
    "SPEAKER_TS_EVAL_VAD_BACKEND",
    "SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES",
    "SPEAKER_TS_EVAL_VAD_HOP_SAMPLES",
    "SPEAKER_TS_EVAL_VAD_OFFSETS",
)


class IndependentRunState:
    """Per-invocation container so A and D cannot share geometry-sensitive state."""

    __slots__ = ("workdir", "config", "instance_id")

    def __init__(self, *, workdir: Path, config: GeometryConfig, instance_id: int) -> None:
        self.workdir = workdir
        self.config = config
        self.instance_id = instance_id


@dataclass(frozen=True)
class CanonicalExecution:
    """Raw slicer output plus private diagnostics. Diagnostics stay off SlicerResult."""

    raw: RawSlicerOutput
    diagnostics: ExecutionDiagnostics


def cutpoints_used_by_selected_schedule(
    cutpoints: Sequence[Any],
    selected_candidates: Sequence[Any],
) -> tuple[Any, ...]:
    """Historical primary population: unique cutpoints used as selected-clip endpoints.

    The detector candidate pool is larger. Public ``SlicerResult.cutpoints`` must be
    this selected-schedule subset, not the full candidate pool. Diagnostics may still
    hash the candidate pool separately.
    """
    selected_ids = {str(candidate.start_cutpoint_id) for candidate in selected_candidates} | {
        str(candidate.end_cutpoint_id) for candidate in selected_candidates
    }
    selected = tuple(cut for cut in cutpoints if str(cut.cutpoint_id) in selected_ids)
    found_ids = {str(cut.cutpoint_id) for cut in selected}
    missing = selected_ids - found_ids
    if missing:
        preview = ", ".join(sorted(missing)[:8])
        raise RuntimeError(
            "selected schedule references cutpoint ids missing from the detector pool: "
            f"{preview}"
        )
    return selected


_NEXT_INSTANCE_ID = 1


def _ensure_speaker_ts_eval_on_path() -> Path:
    src = resolve_speaker_ts_eval_src()
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    return src


def _import_canonical() -> dict[str, Any]:
    """Import canonical detector/packer symbols; fail clearly if unavailable."""
    src = _ensure_speaker_ts_eval_on_path()
    try:
        from speaker_ts_eval.buckeye_safecut_benchmark import (  # type: ignore[import-not-found]
            SourceInfo,
            _compute_recording_vad_frames,
            _index_frames,
        )
        from speaker_ts_eval.repaired_buckeye_benchmark import (  # type: ignore[import-not-found]
            generate_legal_candidate_clips_in_buffers,
            schedule_candidates_by_buffer,
        )
        from speaker_ts_eval.slicer_daddy_test import (  # type: ignore[import-not-found]
            AcousticDetectorContext,
            BenchmarkConfig,
            BenchmarkContexts,
            DetectorContext,
            LinguisticDetectorContext,
            PackingConstraints,
            VadPercentileRmsDetector,
            _build_audio_feature_cache,
            _clips_from_candidates,
            _dedupe_cutpoints,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Canonical slicer path unavailable: install/import speaker_ts_eval "
            f"from {src} (set {SPEAKER_TS_EVAL_SRC_ENV} to that src directory; "
            "plus numpy/torch/silero-vad as needed)."
        ) from exc

    return {
        "SourceInfo": SourceInfo,
        "compute_recording_vad_frames": _compute_recording_vad_frames,
        "index_frames": _index_frames,
        "generate_legal_candidate_clips_in_buffers": generate_legal_candidate_clips_in_buffers,
        "schedule_candidates_by_buffer": schedule_candidates_by_buffer,
        "AcousticDetectorContext": AcousticDetectorContext,
        "BenchmarkConfig": BenchmarkConfig,
        "BenchmarkContexts": BenchmarkContexts,
        "DetectorContext": DetectorContext,
        "LinguisticDetectorContext": LinguisticDetectorContext,
        "PackingConstraints": PackingConstraints,
        "VadPercentileRmsDetector": VadPercentileRmsDetector,
        "build_audio_feature_cache": _build_audio_feature_cache,
        "clips_from_candidates": _clips_from_candidates,
        "dedupe_cutpoints": _dedupe_cutpoints,
    }


def _geometry_env(config: GeometryConfig) -> dict[str, str]:
    return {
        "SPEAKER_TS_EVAL_VAD_BACKEND": config.vad_backend,
        "SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES": str(config.window_samples),
        "SPEAKER_TS_EVAL_VAD_HOP_SAMPLES": str(config.hop_samples),
        "SPEAKER_TS_EVAL_VAD_OFFSETS": ",".join(str(o) for o in config.offsets),
    }


def _probe_wav(path: Path) -> dict[str, Any]:
    import wave

    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        sample_rate = reader.getframerate()
        duration = frames / sample_rate if sample_rate else 0.0
    return {
        "num_frames": frames,
        "sample_rate": sample_rate,
        "duration_sec": duration,
    }


def _flatten_frames(vad_frames_by_recording: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for recording_id in sorted(vad_frames_by_recording):
        frames.extend(vad_frames_by_recording[recording_id])
    return frames


def execute_canonical(request: SlicerRequest) -> RawSlicerOutput:
    """Run vad_percentile_rms + common 3–15s packer for one request.

    Fresh workdir and fresh IndependentRunState every call — A vs D (or any two
    runs) must not share VAD/feature caches or detector contexts.
    """
    return execute_canonical_diagnosed(request).raw


def execute_canonical_diagnosed(request: SlicerRequest) -> CanonicalExecution:
    """Same execution as execute_canonical, plus compact private diagnostics."""
    global _NEXT_INSTANCE_ID
    canon = _import_canonical()
    config = request.config

    if int(request.sample_rate_hz) != int(config.sample_rate_hz):
        raise ValueError(
            f"request sample_rate_hz {request.sample_rate_hz} != config {config.sample_rate_hz}"
        )
    if not request.buffers:
        raise ValueError("SlicerRequest.buffers must be non-empty")

    with tempfile.TemporaryDirectory(prefix=f"buckeye_slicer_{config.name}_") as tmp:
        workdir = Path(tmp)
        instance_id = _NEXT_INSTANCE_ID
        _NEXT_INSTANCE_ID += 1
        run_state = IndependentRunState(workdir=workdir, config=config, instance_id=instance_id)
        assert run_state.instance_id == instance_id
        assert run_state.workdir == workdir

        probe = _probe_wav(request.audio_path)
        if int(probe["sample_rate"]) != int(config.sample_rate_hz):
            raise ValueError(
                f"audio sample rate {probe['sample_rate']} != config {config.sample_rate_hz}"
            )

        SourceInfo = canon["SourceInfo"]
        source = SourceInfo(
            source_audio_id=f"source_{request.recording_id}",
            recording_id=request.recording_id,
            path=request.audio_path,
            duration_sec=float(probe["duration_sec"]),
            sample_rate=int(probe["sample_rate"]),
            num_samples=int(probe["num_frames"]),
        )
        sources = {source.source_audio_id: source}

        buffer_by_id: dict[str, dict[str, Any]] = {}
        for buf in request.buffers:
            buffer_by_id[buf.buffer_id] = {
                "source_audio_id": source.source_audio_id,
                "trusted_start_sec": float(buf.start_sec),
                "trusted_end_sec": float(buf.end_sec),
                "region_type": "adapter_buffer_scope",
            }

        # Per-run dirs only; TemporaryDirectory is discarded after the call.
        # Feature cache files are keyed only by recording_id in the canonical
        # helper — that is why A and D must never share this directory.
        vad_cache_dir = workdir / "vad_frames"
        feature_cache_dir = workdir / "audio_features"
        (workdir / "tables").mkdir(parents=True, exist_ok=True)

        previous_env = {key: os.environ.get(key) for key in _VAD_ENV_KEYS}
        os.environ.update(_geometry_env(config))
        try:
            vad_frames_by_recording = canon["compute_recording_vad_frames"](
                sources, vad_cache_dir
            )
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        frames = _flatten_frames(vad_frames_by_recording)
        vad_observation_count = len(frames)
        vad_timestamp_sha256 = hash_vad_timestamps(frames)
        vad_probability_sha256 = hash_vad_probabilities(frames)
        del frames

        frame_index = {
            recording_id: canon["index_frames"](rec_frames)
            for recording_id, rec_frames in vad_frames_by_recording.items()
        }
        audio_feature_cache = canon["build_audio_feature_cache"](
            sources=sources,
            frame_index_by_recording=frame_index,
            cache_dir=feature_cache_dir,
        )

        benchmark_config = canon["BenchmarkConfig"](
            min_clip_sec=config.min_clip_sec,
            preferred_min_sec=config.preferred_min_sec,
            preferred_max_sec=config.preferred_max_sec,
            target_clip_sec=config.target_clip_sec,
            max_clip_sec=config.max_clip_sec,
            allow_overlap=False,
            max_overlap_sec=0.0,
            deterministic_seed=0,
            vad_threshold=config.vad_threshold,
            vad_min_run_ms=config.vad_min_run_ms,
            percentile_rms_percentile=config.percentile_rms_percentile,
            percentile_rms_margin_db=config.percentile_rms_margin_db,
        )

        shared = canon["DetectorContext"](
            eval_run=workdir,
            normalized_speaker_dir=workdir,
            benchmark_root=workdir,
            config=benchmark_config,
            sources=sources,
            buffer_by_id=buffer_by_id,
            lexical_words_by_recording={},
            speech_phones_by_recording={},
            corrected_safe_regions=[],
            # Empty on purpose: annotations are evaluator-only.
            reference_words=[],
            reference_phones=[],
        )
        acoustic = canon["AcousticDetectorContext"](
            benchmark_root=workdir,
            config=benchmark_config,
            sources=sources,
            buffer_by_id=buffer_by_id,
            audio_feature_cache=audio_feature_cache,
            profiler={},
        )
        linguistic = canon["LinguisticDetectorContext"](
            benchmark_root=workdir,
            config=benchmark_config,
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
        contexts = canon["BenchmarkContexts"](
            shared=shared, acoustic=acoustic, linguistic=linguistic
        )

        # New detector instance every run — no reused mutable detector context.
        detector = canon["VadPercentileRmsDetector"]()
        detector_result = detector.find_cutpoints(contexts)
        cutpoints = canon["dedupe_cutpoints"](detector_result.cutpoints)
        cutpoints = apply_cut_policy(cutpoints, config)

        packing = canon["PackingConstraints"](
            min_clip_sec=benchmark_config.min_clip_sec,
            preferred_min_sec=benchmark_config.preferred_min_sec,
            preferred_max_sec=benchmark_config.preferred_max_sec,
            target_clip_sec=benchmark_config.target_clip_sec,
            max_clip_sec=benchmark_config.max_clip_sec,
            allow_overlap=False,
            max_overlap_sec=0.0,
            max_duplication_ratio=1.0,
            eligible_regions_by_recording={},
        )
        candidates = canon["generate_legal_candidate_clips_in_buffers"](
            cutpoints=cutpoints,
            constraints=packing,
        )
        candidates = apply_candidate_weight_policy(candidates, cutpoints, config)
        selected = canon["schedule_candidates_by_buffer"](candidates, max_overlap_sec=0.0)
        clips = canon["clips_from_candidates"](
            selected, packer_name="optimal_weighted_interval"
        )
        selected_cuts = cutpoints_used_by_selected_schedule(cutpoints, selected)
        if clips and not selected_cuts:
            raise RuntimeError("emitted clips but selected schedule has no cutpoints")

        raw_cuts = tuple(
            RawCutpoint(
                recording_id=cut.recording_id,
                buffer_id=cut.buffer_id,
                time_sec=float(cut.time_sec),
            )
            for cut in selected_cuts
        )
        raw_clips = tuple(
            RawClip(
                recording_id=clip.recording_id,
                buffer_id=clip.buffer_id,
                start_sec=float(clip.start_sec),
                end_sec=float(clip.end_sec),
            )
            for clip in clips
        )
        raw = RawSlicerOutput(cutpoints=raw_cuts, clips=raw_clips)

        diagnostics = ExecutionDiagnostics(
            geometry_name=config.name,
            geometry_canonical=geometry_canonical_json(config),
            geometry_fingerprint=geometry_fingerprint(config),
            instance_id=run_state.instance_id,
            workdir=str(run_state.workdir),
            vad_cache_dir=str(vad_cache_dir),
            feature_cache_dir=str(feature_cache_dir),
            vad_cache_paths=tuple(sorted(str(path) for path in vad_cache_dir.glob("*.json"))),
            feature_cache_paths=tuple(
                sorted(str(path) for path in feature_cache_dir.glob("*.json"))
            ),
            vad_observation_count=vad_observation_count,
            vad_timestamp_sha256=vad_timestamp_sha256,
            vad_probability_sha256=vad_probability_sha256,
            candidate_cutpoint_sha256=hash_cutpoint_times(cutpoints),
            selected_clip_sha256=hash_clip_intervals(raw_clips),
            policy_name=config.name,
            policy_canonical=policy_canonical_json(config),
            policy_fingerprint=policy_fingerprint(config),
            selected_cutpoint_sha256=hash_cutpoint_times(selected_cuts),
        )
        return CanonicalExecution(raw=raw, diagnostics=diagnostics)
