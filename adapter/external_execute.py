"""Turn external detections into the frozen SlicerResult + common packer path."""

from __future__ import annotations

import time
from dataclasses import dataclass, fields
from typing import Any

from adapter.canonical_executor import (
    _import_canonical,
    cutpoints_used_by_selected_schedule,
)
from adapter.config import O0_4
from adapter.convert import to_slicer_result
from adapter.diagnostics import (
    ExecutionDiagnostics,
    hash_clip_intervals,
    hash_cutpoint_times,
)
from adapter.external_baselines import ExternalBaselineSpec
from adapter.external_detect import BufferDetection, ExternalAudioRequest, detect_recording
from adapter.types import RawClip, RawCutpoint, RawSlicerOutput
from referee.types import SlicerResult
from referee.validate import MAX_CLIP_DURATION_SEC, MIN_CLIP_DURATION_SEC


@dataclass(frozen=True)
class NativeLegality:
    raw_clip_count: int
    raw_emitted_sec: float
    clips_lt_3: int
    clips_gt_15: int
    legal_clip_count: int
    native_compatible: bool
    note: str

    def to_dict(self) -> dict[str, object]:
        return {
            "raw_clip_count": self.raw_clip_count,
            "raw_emitted_sec": self.raw_emitted_sec,
            "clips_lt_3": self.clips_lt_3,
            "clips_gt_15": self.clips_gt_15,
            "legal_clip_count": self.legal_clip_count,
            "native_compatible": self.native_compatible,
            "note": self.note,
        }


@dataclass(frozen=True)
class ExternalRun:
    result: SlicerResult
    diagnostics: ExecutionDiagnostics
    legality: NativeLegality
    detections: tuple[BufferDetection, ...]


def _empty_legality(*, note: str) -> NativeLegality:
    return NativeLegality(
        raw_clip_count=0,
        raw_emitted_sec=0.0,
        clips_lt_3=0,
        clips_gt_15=0,
        legal_clip_count=0,
        native_compatible=True,
        note=note,
    )


def classify_native_clips(
    clips: tuple[tuple[str, str, float, float], ...],
) -> tuple[tuple[tuple[str, str, float, float], ...], NativeLegality]:
    legal: list[tuple[str, str, float, float]] = []
    lt3 = 0
    gt15 = 0
    raw_sec = 0.0
    for recording_id, buffer_id, start, end in clips:
        duration = end - start
        raw_sec += duration
        if duration < MIN_CLIP_DURATION_SEC:
            lt3 += 1
            continue
        if duration > MAX_CLIP_DURATION_SEC:
            gt15 += 1
            continue
        legal.append((recording_id, buffer_id, start, end))
    raw_count = len(clips)
    if raw_count == 0:
        note = "native emitted no clips"
        compatible = True
    elif not legal and (lt3 or gt15):
        note = "native output not directly benchmark-compatible"
        compatible = False
    else:
        note = "native legal clips scored without merge/truncate"
        compatible = True
    legality = NativeLegality(
        raw_clip_count=raw_count,
        raw_emitted_sec=raw_sec,
        clips_lt_3=lt3,
        clips_gt_15=gt15,
        legal_clip_count=len(legal),
        native_compatible=compatible,
        note=note,
    )
    return tuple(legal), legality


def _result_from_clips(
    clips: tuple[tuple[str, str, float, float], ...],
    *,
    buffer_bounds: dict[tuple[str, str], tuple[float, float]] | None = None,
) -> SlicerResult:
    cut_keys: list[tuple[str, str, float]] = []
    seen: set[tuple[str, str, float]] = set()
    for recording_id, buffer_id, start, end in clips:
        bounds = None if buffer_bounds is None else buffer_bounds.get((recording_id, buffer_id))
        for time_sec in (start, end):
            if bounds is not None:
                lo, hi = bounds
                if not (lo < time_sec < hi):
                    # Clip edges may sit on allowed-buffer walls; public cutpoints may not.
                    continue
            key = (recording_id, buffer_id, round(time_sec, 9))
            if key in seen:
                continue
            seen.add(key)
            cut_keys.append((recording_id, buffer_id, time_sec))
    raw = RawSlicerOutput(
        cutpoints=tuple(RawCutpoint(*row) for row in cut_keys),
        clips=tuple(RawClip(*row) for row in clips),
    )
    result = to_slicer_result(raw)
    if {f.name for f in fields(result)} != {"cutpoints", "clips"}:
        raise RuntimeError("public result is not a neutral SlicerResult")
    return result


def _make_cutpoint(
    Cutpoint: Any,
    *,
    spec: ExternalBaselineSpec,
    recording_id: str,
    buffer_id: str,
    time_sec: float,
    index: int,
) -> Any:
    return Cutpoint(
        detector_name=spec.name,
        source_id=f"source_{recording_id}",
        recording_id=recording_id,
        buffer_id=buffer_id,
        cutpoint_id=f"{spec.name}:{recording_id}:{buffer_id}:{index:06d}",
        time_sec=float(time_sec),
        interval_start_sec=float(time_sec),
        interval_end_sec=float(time_sec),
        score=1.0,
        vad_min=None,
        vad_mean=None,
        vad_longest_below_0_1_ms=None,
        vad_longest_below_0_2_ms=None,
        rms_min_dbfs=None,
        rms_mean_dbfs=None,
        voicing_min=None,
        voicing_mean=None,
        compute_time_sec=0.0,
        is_buffer_edge_candidate=False,
        metadata={
            "adapter_version": spec.canonical_payload()["adapter_version"],
            "family": spec.family,
            "mode": spec.mode,
            "candidate_rule": spec.candidate_rule,
        },
    )


def pack_common(
    detections: tuple[BufferDetection, ...],
    spec: ExternalBaselineSpec,
) -> tuple[SlicerResult, tuple[Any, ...], tuple[Any, ...]]:
    """Existing frozen 3–15s packer. Candidate timestamps are not altered."""
    canon = _import_canonical()
    Cutpoint = canon["Cutpoint"]
    cutpoints: list[Any] = []
    index = 0
    for detection in detections:
        for time_sec in detection.candidate_times:
            cutpoints.append(
                _make_cutpoint(
                    Cutpoint,
                    spec=spec,
                    recording_id=detection.recording_id,
                    buffer_id=detection.buffer_id,
                    time_sec=time_sec,
                    index=index,
                )
            )
            index += 1
    packing = canon["PackingConstraints"](
        min_clip_sec=O0_4.min_clip_sec,
        preferred_min_sec=O0_4.preferred_min_sec,
        preferred_max_sec=O0_4.preferred_max_sec,
        target_clip_sec=O0_4.target_clip_sec,
        max_clip_sec=O0_4.max_clip_sec,
        allow_overlap=False,
        max_overlap_sec=0.0,
        max_duplication_ratio=1.0,
        eligible_regions_by_recording={},
    )
    candidates = canon["generate_legal_candidate_clips_in_buffers"](
        cutpoints=cutpoints,
        constraints=packing,
    )
    selected = canon["schedule_candidates_by_buffer"](candidates, max_overlap_sec=0.0)
    clips = canon["clips_from_candidates"](
        selected, packer_name="optimal_weighted_interval"
    )
    selected_cuts = cutpoints_used_by_selected_schedule(cutpoints, selected)
    if clips and not selected_cuts:
        raise RuntimeError("emitted clips but selected schedule has no cutpoints")
    raw = RawSlicerOutput(
        cutpoints=tuple(
            RawCutpoint(
                recording_id=cut.recording_id,
                buffer_id=cut.buffer_id,
                time_sec=float(cut.time_sec),
            )
            for cut in selected_cuts
        ),
        clips=tuple(
            RawClip(
                recording_id=clip.recording_id,
                buffer_id=clip.buffer_id,
                start_sec=float(clip.start_sec),
                end_sec=float(clip.end_sec),
            )
            for clip in clips
        ),
    )
    result = to_slicer_result(raw)
    if {f.name for f in fields(result)} != {"cutpoints", "clips"}:
        raise RuntimeError("public result is not a neutral SlicerResult")
    for clip in result.clips:
        if "|" in clip.buffer_id:
            raise RuntimeError(f"cross-buffer clip buffer_id: {clip.buffer_id}")
        duration = clip.end_sec - clip.start_sec
        if duration < MIN_CLIP_DURATION_SEC or duration > MAX_CLIP_DURATION_SEC:
            raise RuntimeError(f"common packer emitted illegal duration {duration}")
    return result, tuple(cutpoints), tuple(selected_cuts)


def _diagnostics(
    *,
    spec: ExternalBaselineSpec,
    result: SlicerResult,
    candidate_cuts: tuple[Any, ...],
    selected_cuts: tuple[Any, ...],
    detector_sec: float,
    pack_sec: float,
) -> ExecutionDiagnostics:
    return ExecutionDiagnostics(
        geometry_name=spec.name,
        geometry_canonical="",
        geometry_fingerprint=spec.fingerprint(),
        instance_id=1,
        workdir="",
        vad_cache_dir="",
        feature_cache_dir="",
        vad_cache_paths=(),
        feature_cache_paths=(),
        vad_observation_count=0,
        vad_timestamp_sha256="external",
        vad_probability_sha256="external",
        candidate_cutpoint_sha256=hash_cutpoint_times(candidate_cuts),
        selected_clip_sha256=hash_clip_intervals(result.clips),
        policy_name=spec.name,
        policy_canonical="",
        policy_fingerprint=spec.fingerprint(),
        selected_cutpoint_sha256=hash_cutpoint_times(selected_cuts),
        vad_compute_count=0,
        feature_bundle_id="",
        vad_compute_sec=float(detector_sec),
        policy_eval_sec=float(pack_sec),
    )


def execute_external(request: ExternalAudioRequest) -> ExternalRun:
    request.assert_firewall()
    detections = detect_recording(request)
    detector_sec = sum(item.detector_runtime_sec for item in detections)
    spec = request.spec
    if spec.mode == "native":
        raw_clips = tuple(
            (item.recording_id, item.buffer_id, start, end)
            for item in detections
            for start, end in item.native_clips
        )
        legal, legality = classify_native_clips(raw_clips)
        bounds = {
            (item.recording_id, item.buffer_id): (item.buffer_start_sec, item.buffer_end_sec)
            for item in detections
        }
        result = _result_from_clips(legal, buffer_bounds=bounds)
        candidate_cuts = tuple(
            RawCutpoint(item.recording_id, item.buffer_id, time_sec)
            for item in detections
            for time_sec in item.candidate_times
        )
        selected_cuts = result.cutpoints
        pack_sec = 0.0
    elif spec.mode == "common_packer":
        pack_started = time.perf_counter()
        result, candidate_cuts, selected_cuts = pack_common(detections, spec)
        pack_sec = time.perf_counter() - pack_started
        legality = _empty_legality(note="common packer; native legality tracked on native rows")
    else:
        raise RuntimeError(f"unknown external mode {spec.mode!r}")
    diagnostics = _diagnostics(
        spec=spec,
        result=result,
        candidate_cuts=candidate_cuts,
        selected_cuts=selected_cuts,
        detector_sec=detector_sec,
        pack_sec=pack_sec,
    )
    return ExternalRun(
        result=result,
        diagnostics=diagnostics,
        legality=legality,
        detections=detections,
    )
