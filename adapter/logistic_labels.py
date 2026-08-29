"""Evaluator labels for O0_4 detector candidates. Not slicer inputs.

Outside a trusted phone is not demonstrated unsafe, and is not automatically
safe. Training uses three states; inference still scores every candidate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from referee.evaluate import (
    DEPTH_GT_20MS,
    DEPTH_GT_50MS,
    _depth_gt_threshold_ms,
    _depth_sec_to_us,
    _leak_depth_inside_phone,
)
from referee.types import BufferScope, PhoneInterval, RecordingReference, UncertaintyInterval

STATE_BAD = "bad"
STATE_SAFE = "safe"
STATE_UNKNOWN = "unknown"
TRAIN_STATES = frozenset({STATE_BAD, STATE_SAFE})
PHASE10B_STATE_TARGETS: tuple[str, ...] = ("inside", "gt20ms", "gt50ms")
LABEL_SCHEMA_ID = "phase10b_training_labels_v1"
SAFE_PAUSE_MARGIN_MS = 20


def phones_for_buffer(
    phones: Sequence[PhoneInterval],
    *,
    recording_id: str,
    buffer_id: str,
) -> list[PhoneInterval]:
    return [
        phone
        for phone in phones
        if phone.recording_id == recording_id and phone.buffer_id == buffer_id
    ]


def uncertainty_for_buffer(
    masks: Sequence[UncertaintyInterval],
    *,
    recording_id: str,
    buffer_id: str,
) -> list[UncertaintyInterval]:
    return [
        mask
        for mask in masks
        if mask.recording_id == recording_id and mask.buffer_id == buffer_id
    ]


def y_from_state(state: str) -> int | None:
    if state == STATE_BAD:
        return 1
    if state == STATE_SAFE:
        return 0
    if state == STATE_UNKNOWN:
        return None
    raise RuntimeError(f"unknown training state {state!r}")


def _inside_open_interval(time_sec: float, start_sec: float, end_sec: float) -> bool:
    return float(start_sec) < float(time_sec) < float(end_sec)


def _in_uncertainty(time_sec: float, masks: Sequence[UncertaintyInterval]) -> bool:
    return any(_inside_open_interval(time_sec, mask.start_sec, mask.end_sec) for mask in masks)


def _in_allowed_buffer(time_sec: float, buffers: Sequence[BufferScope], buffer_id: str) -> bool:
    for buf in buffers:
        if buf.buffer_id == buffer_id:
            return _inside_open_interval(time_sec, buf.start_sec, buf.end_sec)
    return False


def _annotated_span(phones: Sequence[PhoneInterval]) -> tuple[float, float] | None:
    if not phones:
        return None
    return (min(phone.start_sec for phone in phones), max(phone.end_sec for phone in phones))


def _phone_distance_sec(time_sec: float, phone: PhoneInterval) -> float:
    t = float(time_sec)
    if t < float(phone.start_sec):
        return float(phone.start_sec) - t
    if t > float(phone.end_sec):
        return t - float(phone.end_sec)
    return 0.0


def is_trusted_pause(
    time_sec: float,
    *,
    phones: Sequence[PhoneInterval],
    masks: Sequence[UncertaintyInterval],
    buffers: Sequence[BufferScope],
    buffer_id: str,
) -> bool:
    """True only in high-precision pause territory.

    Exact phone endpoints are unknown, not safe. Safe requires a closed gap
    at least SAFE_PAUSE_MARGIN_MS from every phone interval.
    """
    if not _in_allowed_buffer(time_sec, buffers, buffer_id):
        return False
    if _in_uncertainty(time_sec, masks):
        return False
    span = _annotated_span(phones)
    if span is None:
        return False
    t = float(time_sec)
    if not (span[0] <= t <= span[1]):
        return False
    nearest = min((_phone_distance_sec(t, phone) for phone in phones), default=0.0)
    return _depth_sec_to_us(nearest) >= SAFE_PAUSE_MARGIN_MS * 1000


def _state_for_target(*, is_bad: bool, trusted_pause: bool) -> str:
    if is_bad:
        return STATE_BAD
    if trusted_pause:
        return STATE_SAFE
    return STATE_UNKNOWN


def label_candidate(
    time_sec: float,
    *,
    recording_id: str,
    buffer_id: str,
    reference: RecordingReference,
) -> dict[str, Any]:
    """Return per-target state and train labels. y is None for unknown."""
    phones = phones_for_buffer(
        reference.phones, recording_id=recording_id, buffer_id=buffer_id
    )
    masks = uncertainty_for_buffer(
        reference.uncertainty_intervals, recording_id=recording_id, buffer_id=buffer_id
    )
    depth = _leak_depth_inside_phone(float(time_sec), phones)
    trusted_pause = is_trusted_pause(
        time_sec,
        phones=phones,
        masks=masks,
        buffers=reference.buffers,
        buffer_id=buffer_id,
    )
    inside_bad = depth is not None
    gt20_bad = depth is not None and _depth_gt_threshold_ms(depth, DEPTH_GT_20MS)
    gt50_bad = depth is not None and _depth_gt_threshold_ms(depth, DEPTH_GT_50MS)
    states = {
        "state_inside": _state_for_target(is_bad=inside_bad, trusted_pause=trusted_pause),
        "state_gt20ms": _state_for_target(is_bad=gt20_bad, trusted_pause=trusted_pause),
        "state_gt50ms": _state_for_target(is_bad=gt50_bad, trusted_pause=trusted_pause),
    }
    labels: dict[str, Any] = dict(states)
    for target in PHASE10B_STATE_TARGETS:
        labels[f"y_{target}"] = y_from_state(str(states[f"state_{target}"]))
    return labels


def count_label_states(rows: Sequence[dict[str, object]]) -> dict[str, dict[str, int]]:
    counts = {
        target: {STATE_BAD: 0, STATE_SAFE: 0, STATE_UNKNOWN: 0}
        for target in PHASE10B_STATE_TARGETS
    }
    for row in rows:
        for target in PHASE10B_STATE_TARGETS:
            state = str(row[f"state_{target}"])
            if state not in counts[target]:
                raise RuntimeError(f"invalid state {state!r} for {target}")
            counts[target][state] += 1
    return counts
