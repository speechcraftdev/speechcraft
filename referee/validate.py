"""Loud validation of RecordingReference + SlicerResult invariants."""

from __future__ import annotations

from referee.intervals import intervals_overlap
from referee.types import (
    BufferScope,
    Clip,
    PhoneInterval,
    RecordingReference,
    SlicerResult,
)

MIN_CLIP_DURATION_SEC = 3.0
MAX_CLIP_DURATION_SEC = 15.0


class ValidationError(ValueError):
    """Malformed benchmark input; do not silently normalize."""


def _buffer_map(reference: RecordingReference) -> dict[str, BufferScope]:
    by_id: dict[str, BufferScope] = {}
    for buf in reference.buffers:
        if buf.buffer_id in by_id:
            raise ValidationError(
                f"duplicate buffer_id {buf.buffer_id!r} within recording {reference.recording_id!r}"
            )
        if not (buf.start_sec < buf.end_sec):
            raise ValidationError(
                f"buffer {buf.buffer_id!r}: start_sec ({buf.start_sec}) must be < end_sec ({buf.end_sec})"
            )
        by_id[buf.buffer_id] = buf
    return by_id


def _require_known_buffer(
    *,
    buffer_id: str,
    buffers: dict[str, BufferScope],
    label: str,
) -> BufferScope:
    try:
        return buffers[buffer_id]
    except KeyError as exc:
        raise ValidationError(f"{label}: unknown buffer_id {buffer_id!r}") from exc


def _require_within_buffer(
    *,
    start_sec: float,
    end_sec: float,
    buffer: BufferScope,
    label: str,
) -> None:
    if start_sec < buffer.start_sec or end_sec > buffer.end_sec:
        raise ValidationError(
            f"{label}: interval [{start_sec}, {end_sec}] is outside buffer "
            f"{buffer.buffer_id!r} scope [{buffer.start_sec}, {buffer.end_sec}]"
        )


def _validate_phones(
    reference: RecordingReference,
    buffers: dict[str, BufferScope],
) -> None:
    by_buffer: dict[str, list[PhoneInterval]] = {}
    for phone in reference.phones:
        if phone.recording_id != reference.recording_id:
            raise ValidationError(
                f"phone recording_id {phone.recording_id!r} != reference {reference.recording_id!r}"
            )
        if not (phone.start_sec < phone.end_sec):
            raise ValidationError(
                f"phone {phone.phone!r}: start_sec ({phone.start_sec}) must be < end_sec ({phone.end_sec})"
            )
        buf = _require_known_buffer(
            buffer_id=phone.buffer_id, buffers=buffers, label="phone"
        )
        _require_within_buffer(
            start_sec=phone.start_sec,
            end_sec=phone.end_sec,
            buffer=buf,
            label=f"phone {phone.phone!r}",
        )
        by_buffer.setdefault(phone.buffer_id, []).append(phone)

    # Canonical Buckeye trusted phones within one buffer must not overlap.
    for buffer_id, phones in by_buffer.items():
        ordered = sorted(phones, key=lambda p: (p.start_sec, p.end_sec))
        for prev, cur in zip(ordered, ordered[1:]):
            if intervals_overlap((prev.start_sec, prev.end_sec), (cur.start_sec, cur.end_sec)):
                raise ValidationError(
                    f"overlapping trusted phones in buffer {buffer_id!r}: "
                    f"[{prev.start_sec}, {prev.end_sec}] ({prev.phone!r}) and "
                    f"[{cur.start_sec}, {cur.end_sec}] ({cur.phone!r})"
                )


def _validate_uncertainty(
    reference: RecordingReference,
    buffers: dict[str, BufferScope],
) -> None:
    for mask in reference.uncertainty_intervals:
        if mask.recording_id != reference.recording_id:
            raise ValidationError(
                f"uncertainty recording_id {mask.recording_id!r} != reference {reference.recording_id!r}"
            )
        if not (mask.start_sec < mask.end_sec):
            raise ValidationError(
                f"uncertainty: start_sec ({mask.start_sec}) must be < end_sec ({mask.end_sec})"
            )
        buf = _require_known_buffer(
            buffer_id=mask.buffer_id, buffers=buffers, label="uncertainty"
        )
        _require_within_buffer(
            start_sec=mask.start_sec,
            end_sec=mask.end_sec,
            buffer=buf,
            label="uncertainty",
        )


def _validate_cutpoints(
    reference: RecordingReference,
    result: SlicerResult,
    buffers: dict[str, BufferScope],
) -> None:
    for cut in result.cutpoints:
        if cut.recording_id != reference.recording_id:
            raise ValidationError(
                f"cutpoint recording_id {cut.recording_id!r} != reference {reference.recording_id!r}"
            )
        buf = _require_known_buffer(
            buffer_id=cut.buffer_id, buffers=buffers, label="cutpoint"
        )
        # Raw buffer edges are not legal detector cutpoints.
        if not (buf.start_sec < cut.time_sec < buf.end_sec):
            raise ValidationError(
                f"cutpoint time {cut.time_sec} is not strictly inside buffer "
                f"{buf.buffer_id!r} ({buf.start_sec}, {buf.end_sec})"
            )


def _validate_clips(
    reference: RecordingReference,
    result: SlicerResult,
    buffers: dict[str, BufferScope],
) -> None:
    seen: set[Clip] = set()
    for clip in result.clips:
        if clip.recording_id != reference.recording_id:
            raise ValidationError(
                f"clip recording_id {clip.recording_id!r} != reference {reference.recording_id!r}"
            )
        if not (clip.start_sec < clip.end_sec):
            raise ValidationError(
                f"clip: start_sec ({clip.start_sec}) must be < end_sec ({clip.end_sec})"
            )
        buf = _require_known_buffer(
            buffer_id=clip.buffer_id, buffers=buffers, label="clip"
        )
        _require_within_buffer(
            start_sec=clip.start_sec,
            end_sec=clip.end_sec,
            buffer=buf,
            label="clip",
        )
        duration = clip.end_sec - clip.start_sec
        if duration < MIN_CLIP_DURATION_SEC:
            raise ValidationError(
                f"clip duration {duration} < minimum {MIN_CLIP_DURATION_SEC}"
            )
        if duration > MAX_CLIP_DURATION_SEC:
            raise ValidationError(
                f"clip duration {duration} > maximum {MAX_CLIP_DURATION_SEC}"
            )
        if clip in seen:
            raise ValidationError(f"duplicate clip row: {clip}")
        seen.add(clip)


def validate(reference: RecordingReference, result: SlicerResult) -> dict[str, BufferScope]:
    """Validate reference + slicer output. Returns buffer_id -> BufferScope map."""
    if not reference.buffers:
        raise ValidationError("recording must declare at least one buffer")
    buffers = _buffer_map(reference)
    _validate_phones(reference, buffers)
    _validate_uncertainty(reference, buffers)
    _validate_cutpoints(reference, result, buffers)
    _validate_clips(reference, result, buffers)
    return buffers
