"""Convert raw slicer output to Phase-1 SlicerResult only."""

from __future__ import annotations

from adapter.types import RawClip, RawCutpoint, RawSlicerOutput
from referee.types import Clip, Cutpoint, SlicerResult


def to_slicer_result(raw: RawSlicerOutput) -> SlicerResult:
    """Map raw cuts/clips to the neutral Phase-1 contract (cutpoints + clips)."""
    cutpoints = tuple(
        Cutpoint(
            recording_id=cut.recording_id,
            buffer_id=cut.buffer_id,
            time_sec=cut.time_sec,
        )
        for cut in raw.cutpoints
    )
    clips = tuple(
        Clip(
            recording_id=clip.recording_id,
            buffer_id=clip.buffer_id,
            start_sec=clip.start_sec,
            end_sec=clip.end_sec,
        )
        for clip in raw.clips
    )
    return SlicerResult(cutpoints=cutpoints, clips=clips)


def raw_from_pairs(
    *,
    cuts: list[tuple[str, str, float]],
    clips: list[tuple[str, str, float, float]],
) -> RawSlicerOutput:
    """Tiny helper for tests: (rec, buf, t) / (rec, buf, start, end) tuples."""
    return RawSlicerOutput(
        cutpoints=tuple(RawCutpoint(*row) for row in cuts),
        clips=tuple(RawClip(*row) for row in clips),
    )
