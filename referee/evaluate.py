"""Pure evaluator: RecordingReference + SlicerResult -> metrics.

Knows nothing about VAD, RMS, geometry, detectors, or Buckeye parsing.
"""

from __future__ import annotations

from referee.intervals import duration, merge_intervals, subtract_union
from referee.types import (
    Clip,
    Cutpoint,
    EdgeSafetyMetrics,
    EvaluationResult,
    PhoneInterval,
    RecordingReference,
    SlicerResult,
    UniqueCutSafetyMetrics,
)
from referee.validate import validate

DEPTH_GT_20MS = 0.020
DEPTH_GT_50MS = 0.050
DEPTH_GT_100MS = 0.100


def _rate(count: int, total: int) -> float | None:
    if total == 0:
        return None
    return count / total


def _leak_depth_inside_phone(
    time_sec: float,
    phones: list[PhoneInterval],
) -> float | None:
    """Return leak depth if strictly inside a trusted phone; else None.

    Phone [a, b]: cut at exactly a or b is NOT inside; only a < t < b.
    leak_depth_sec = min(t - a, b - t).
    """
    for phone in phones:
        if phone.start_sec < time_sec < phone.end_sec:
            return min(time_sec - phone.start_sec, phone.end_sec - time_sec)
    return None


def _safety_counts(
    times: list[tuple[str, str, float]],
    phones_by_buffer: dict[tuple[str, str], list[PhoneInterval]],
) -> tuple[int, int, int, int, int]:
    """Return (n, inside, gt20, gt50, gt100) for a list of (rec, buf, t)."""
    inside = 0
    gt20 = 0
    gt50 = 0
    gt100 = 0
    for recording_id, buffer_id, time_sec in times:
        depth = _leak_depth_inside_phone(
            time_sec, phones_by_buffer.get((recording_id, buffer_id), [])
        )
        if depth is None:
            continue
        inside += 1
        if depth > DEPTH_GT_20MS:
            gt20 += 1
        if depth > DEPTH_GT_50MS:
            gt50 += 1
        if depth > DEPTH_GT_100MS:
            gt100 += 1
    return len(times), inside, gt20, gt50, gt100


def _unique_cutpoints(cutpoints: tuple[Cutpoint, ...]) -> list[tuple[str, str, float]]:
    seen: set[tuple[str, str, float]] = set()
    unique: list[tuple[str, str, float]] = []
    for cut in cutpoints:
        key = (cut.recording_id, cut.buffer_id, cut.time_sec)
        if key in seen:
            continue
        seen.add(key)
        unique.append(key)
    return unique


def _final_edge_occurrences(clips: tuple[Clip, ...]) -> list[tuple[str, str, float]]:
    edges: list[tuple[str, str, float]] = []
    for clip in clips:
        edges.append((clip.recording_id, clip.buffer_id, clip.start_sec))
        edges.append((clip.recording_id, clip.buffer_id, clip.end_sec))
    return edges


def _eligible_intervals_by_buffer(
    reference: RecordingReference,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    phones_by_buf: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for phone in reference.phones:
        key = (phone.recording_id, phone.buffer_id)
        phones_by_buf.setdefault(key, []).append((phone.start_sec, phone.end_sec))

    masks_by_buf: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for mask in reference.uncertainty_intervals:
        key = (mask.recording_id, mask.buffer_id)
        masks_by_buf.setdefault(key, []).append((mask.start_sec, mask.end_sec))

    eligible: dict[tuple[str, str], list[tuple[float, float]]] = {}
    all_keys = set(phones_by_buf) | set(masks_by_buf)
    for key in all_keys:
        phone_ivs = merge_intervals(phones_by_buf.get(key, []))
        mask_ivs = masks_by_buf.get(key, [])
        eligible[key] = subtract_union(phone_ivs, mask_ivs)
    return eligible


def _retained_target_speech_sec(
    eligible_by_buf: dict[tuple[str, str], list[tuple[float, float]]],
    clips: tuple[Clip, ...],
) -> float:
    clips_by_buf: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for clip in clips:
        key = (clip.recording_id, clip.buffer_id)
        clips_by_buf.setdefault(key, []).append((clip.start_sec, clip.end_sec))

    retained = 0.0
    for key, eligible in eligible_by_buf.items():
        clip_union = merge_intervals(clips_by_buf.get(key, []))
        # retained = duration(eligible ∩ union(clips))
        covered: list[tuple[float, float]] = []
        for e_start, e_end in eligible:
            for c_start, c_end in clip_union:
                start = max(e_start, c_start)
                end = min(e_end, c_end)
                if start < end:
                    covered.append((start, end))
        retained += duration(merge_intervals(covered))
    return retained


def evaluate(reference: RecordingReference, result: SlicerResult) -> EvaluationResult:
    validate(reference, result)

    phones_by_buffer: dict[tuple[str, str], list[PhoneInterval]] = {}
    for phone in reference.phones:
        phones_by_buffer.setdefault((phone.recording_id, phone.buffer_id), []).append(phone)

    unique_times = _unique_cutpoints(result.cutpoints)
    n_u, inside_u, gt20_u, gt50_u, gt100_u = _safety_counts(unique_times, phones_by_buffer)
    unique_cut_safety = UniqueCutSafetyMetrics(
        unique_cutpoint_count=n_u,
        inside_phone_count=inside_u,
        inside_phone_rate=_rate(inside_u, n_u),
        depth_gt_20ms_count=gt20_u,
        depth_gt_50ms_count=gt50_u,
        depth_gt_100ms_count=gt100_u,
        depth_gt_20ms_rate=_rate(gt20_u, n_u),
        depth_gt_50ms_rate=_rate(gt50_u, n_u),
        depth_gt_100ms_rate=_rate(gt100_u, n_u),
    )

    edge_times = _final_edge_occurrences(result.clips)
    n_e, inside_e, gt20_e, gt50_e, gt100_e = _safety_counts(edge_times, phones_by_buffer)
    final_edge_safety = EdgeSafetyMetrics(
        edge_occurrence_count=n_e,
        inside_phone_count=inside_e,
        inside_phone_rate=_rate(inside_e, n_e),
        depth_gt_20ms_count=gt20_e,
        depth_gt_50ms_count=gt50_e,
        depth_gt_100ms_count=gt100_e,
        depth_gt_20ms_rate=_rate(gt20_e, n_e),
        depth_gt_50ms_rate=_rate(gt50_e, n_e),
        depth_gt_100ms_rate=_rate(gt100_e, n_e),
    )

    eligible_by_buf = _eligible_intervals_by_buffer(reference)
    eligible_sec = sum(duration(ivs) for ivs in eligible_by_buf.values())
    retained_sec = _retained_target_speech_sec(eligible_by_buf, result.clips)
    speech_coverage = None if eligible_sec == 0.0 else retained_sec / eligible_sec

    emitted_audio_sec = sum(clip.end_sec - clip.start_sec for clip in result.clips)

    return EvaluationResult(
        unique_cut_safety=unique_cut_safety,
        final_edge_safety=final_edge_safety,
        eligible_target_speech_sec=eligible_sec,
        retained_target_speech_sec=retained_sec,
        speech_coverage=speech_coverage,
        emitted_audio_sec=emitted_audio_sec,
        clip_count=len(result.clips),
    )
