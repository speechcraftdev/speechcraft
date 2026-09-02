"""Signed-margin labels for O0_4 detector candidates. Not slicer inputs.

Train on every candidate inside trusted annotation coverage and outside
uncertainty, including the ambiguous middle. Phone endpoints are zero, not
unknown. Giant pauses are capped so they do not dominate the fit.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from referee.evaluate import _depth_sec_to_us, _leak_depth_inside_phone
from referee.types import BufferScope, PhoneInterval, RecordingReference, UncertaintyInterval

MARGIN_SCHEMA_ID = "phase10c_signed_margin_v1"
MARGIN_CAP_MS = 100
MARGIN_MODEL_ID = "O0_4_MARGIN_all"
UNKNOWN_REASONS: tuple[str, ...] = (
    "unknown_outside_buffer",
    "unknown_uncertainty",
    "unknown_no_phones",
    "unknown_before_annotated_span",
    "unknown_after_annotated_span",
)
USABLE_REASONS: tuple[str, ...] = (
    "usable_inside",
    "usable_edge",
    "usable_outside",
)
MARGIN_REASONS: tuple[str, ...] = UNKNOWN_REASONS + USABLE_REASONS


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


def _capped_ms(duration_sec: float) -> int:
    ms = int(round(_depth_sec_to_us(abs(float(duration_sec))) / 1000.0))
    return min(MARGIN_CAP_MS, max(0, ms))


def classify_margin(
    time_sec: float,
    *,
    recording_id: str,
    buffer_id: str,
    reference: RecordingReference,
) -> tuple[int | None, str]:
    """Return (capped signed margin or None, exclusive reason)."""
    phones = phones_for_buffer(
        reference.phones, recording_id=recording_id, buffer_id=buffer_id
    )
    masks = uncertainty_for_buffer(
        reference.uncertainty_intervals, recording_id=recording_id, buffer_id=buffer_id
    )
    if not _in_allowed_buffer(time_sec, reference.buffers, buffer_id):
        return None, "unknown_outside_buffer"
    if _in_uncertainty(time_sec, masks):
        return None, "unknown_uncertainty"
    span = _annotated_span(phones)
    if span is None:
        return None, "unknown_no_phones"
    t = float(time_sec)
    if t < span[0]:
        return None, "unknown_before_annotated_span"
    if t > span[1]:
        return None, "unknown_after_annotated_span"
    depth = _leak_depth_inside_phone(t, phones)
    if depth is not None:
        return -_capped_ms(depth), "usable_inside"
    nearest = min((_phone_distance_sec(t, phone) for phone in phones), default=0.0)
    if nearest <= 0.0:
        return 0, "usable_edge"
    return _capped_ms(nearest), "usable_outside"


def signed_margin_ms(
    time_sec: float,
    *,
    recording_id: str,
    buffer_id: str,
    reference: RecordingReference,
) -> int | None:
    """Return capped signed margin in milliseconds, or None if not train-usable.

    Negative = inside-phone penetration. Zero = phone edge. Positive = distance
    to the nearest phone. None = uncertainty, outside the annotated span, or
    outside the allowed buffer.
    """
    margin, _reason = classify_margin(
        time_sec,
        recording_id=recording_id,
        buffer_id=buffer_id,
        reference=reference,
    )
    return margin


def count_margin_labels(rows: Sequence[dict[str, object]]) -> dict[str, int]:
    usable = 0
    unknown = 0
    inside = 0
    edge = 0
    outside = 0
    for row in rows:
        raw = row.get("y_margin_ms")
        if raw is None or raw == "":
            unknown += 1
            continue
        usable += 1
        value = int(raw)
        if value < 0:
            inside += 1
        elif value > 0:
            outside += 1
        else:
            edge += 1
    return {
        "usable": usable,
        "unknown": unknown,
        "inside": inside,
        "edge": edge,
        "outside": outside,
    }


def count_margin_reasons(rows: Sequence[dict[str, object]]) -> dict[str, int]:
    counts = {reason: 0 for reason in MARGIN_REASONS}
    for row in rows:
        reason = str(row.get("margin_reason") or "")
        if reason not in counts:
            raise RuntimeError(f"unknown margin_reason {reason!r}")
        counts[reason] += 1
    return counts


def attach_margin_labels(
    rows: Sequence[dict[str, object]],
    *,
    references: dict[tuple[str, str], RecordingReference],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]))
        reference = references.get(key)
        if reference is None:
            raise RuntimeError(f"missing reference for {key[0]}/{key[1]}")
        margin, reason = classify_margin(
            float(row["time_sec"]),
            recording_id=str(row["recording_id"]),
            buffer_id=str(row["buffer_id"]),
            reference=reference,
        )
        item = dict(row)
        item["y_margin_ms"] = margin
        item["margin_usable"] = 0 if margin is None else 1
        item["margin_reason"] = reason
        item["margin_schema_id"] = MARGIN_SCHEMA_ID
        out.append(item)
    return out
