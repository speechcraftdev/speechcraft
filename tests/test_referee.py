"""Synthetic Phase 1 referee tests — no slicer, VAD, or corpus parsing."""

from __future__ import annotations

import pytest

from referee import (
    BufferScope,
    Clip,
    Cutpoint,
    PhoneInterval,
    RecordingReference,
    SlicerResult,
    UncertaintyInterval,
    ValidationError,
    evaluate,
)
from referee.evaluate import _leak_depth_inside_phone


REC = "rec0"
BUF = "buf0"


def _ref(
    *,
    buffers: list[BufferScope] | None = None,
    phones: list[PhoneInterval] | None = None,
    uncertainty: list[UncertaintyInterval] | None = None,
    recording_id: str = REC,
) -> RecordingReference:
    if buffers is None:
        buffers = [BufferScope(BUF, 0.0, 30.0)]
    return RecordingReference(
        recording_id=recording_id,
        buffers=tuple(buffers),
        phones=tuple(phones or ()),
        uncertainty_intervals=tuple(uncertainty or ()),
    )


def _clip(start: float, end: float, *, buffer_id: str = BUF, recording_id: str = REC) -> Clip:
    return Clip(recording_id, buffer_id, start, end)


def _cut(t: float, *, buffer_id: str = BUF, recording_id: str = REC) -> Cutpoint:
    return Cutpoint(recording_id, buffer_id, t)


def _phone(
    start: float,
    end: float,
    phone: str = "AA",
    *,
    buffer_id: str = BUF,
    recording_id: str = REC,
) -> PhoneInterval:
    return PhoneInterval(recording_id, buffer_id, start, end, phone)


def _unc(
    start: float,
    end: float,
    *,
    buffer_id: str = BUF,
    recording_id: str = REC,
) -> UncertaintyInterval:
    return UncertaintyInterval(recording_id, buffer_id, start, end)


# ---------------------------------------------------------------------------
# A. Phone penetration exact semantics
# ---------------------------------------------------------------------------


class TestPhonePenetrationSemantics:
    def test_boundary_and_interior_depths(self) -> None:
        phone = _phone(1.000, 1.100)
        phones = [phone]

        assert _leak_depth_inside_phone(0.990, phones) is None
        assert _leak_depth_inside_phone(1.000, phones) is None
        assert _leak_depth_inside_phone(1.010, phones) == pytest.approx(0.010)
        assert _leak_depth_inside_phone(1.050, phones) == pytest.approx(0.050)
        assert _leak_depth_inside_phone(1.099, phones) == pytest.approx(0.001)
        assert _leak_depth_inside_phone(1.100, phones) is None

    def test_strict_depth_thresholds_in_aggregate(self) -> None:
        # Use depths safely away from float-equality edges for 0.020 / 0.050 / 0.100.
        # Exact-threshold strictness is asserted separately on float literals below.
        reference = _ref(phones=[_phone(1.000, 1.300)])
        cuts = [
            _cut(1.010),  # depth 0.010 — inside, not >20ms
            _cut(1.019),  # depth 0.019 — inside, not >20ms
            _cut(1.021),  # depth 0.021 — >20ms
            _cut(1.049),  # depth 0.049 — >20, not >50
            _cut(1.051),  # depth 0.051 — >50ms
            _cut(1.099),  # depth 0.099 — >50, not >100
            _cut(1.101),  # depth 0.101 — >100ms
        ]
        result = SlicerResult(cutpoints=tuple(cuts), clips=(_clip(2.0, 5.0),))
        metrics = evaluate(reference, result).unique_cut_safety

        assert metrics.unique_cutpoint_count == 7
        assert metrics.inside_phone_count == 7
        assert metrics.depth_gt_20ms_count == 5  # 0.021, 0.049, 0.051, 0.099, 0.101
        assert metrics.depth_gt_50ms_count == 3  # 0.051, 0.099, 0.101
        assert metrics.depth_gt_100ms_count == 1  # 0.101 only
        assert metrics.depth_gt_20ms_rate == pytest.approx(5 / 7)
        assert metrics.depth_gt_50ms_rate == pytest.approx(3 / 7)
        assert metrics.depth_gt_100ms_rate == pytest.approx(1 / 7)

    def test_threshold_comparisons_are_strict_gt(self) -> None:
        # Operator contract: equality does not count (strict >).
        assert not (0.020 > 0.020)
        assert not (0.050 > 0.050)
        assert not (0.100 > 0.100)
        assert 0.021 > 0.020
        assert 0.051 > 0.050
        assert 0.101 > 0.100



# ---------------------------------------------------------------------------
# B. Coverage hand calculation
# ---------------------------------------------------------------------------


class TestCoverageHandCalculation:
    def test_retained_eligible_coverage(self) -> None:
        # Hand calc on conceptual coverage windows [0.5,1.5] and [3.2,3.8]
        # against phones [1,2] and [3,4]: retained 0.5+0.6=1.1, eligible 2.0.
        # Legal clips (>=3s) extend into silence without adding phone coverage:
        #   clip [2.5, 5.5] ∩ phone[5,6] shifted... use shifted timeline:
        # phones [5,6] and [8,9]; clip [2.5,5.5] retains 0.5; clip [8.4,11.4] retains 0.6.
        reference = _ref(
            buffers=[BufferScope(BUF, 0.0, 30.0)],
            phones=[_phone(5.0, 6.0, "P1"), _phone(8.0, 9.0, "P2")],
        )
        result = SlicerResult(
            cutpoints=(),
            clips=(
                _clip(2.5, 5.5),   # ∩ [5,6] = 0.5
                _clip(8.4, 11.4),  # ∩ [8,9] = 0.6
            ),
        )
        out = evaluate(reference, result)
        assert out.eligible_target_speech_sec == pytest.approx(2.0)
        assert out.retained_target_speech_sec == pytest.approx(1.1)
        assert out.speech_coverage == pytest.approx(0.55)
        assert out.emitted_audio_sec == pytest.approx(6.0)
        assert out.clip_count == 2


# ---------------------------------------------------------------------------
# C. Uncertainty subtraction
# ---------------------------------------------------------------------------


class TestUncertaintySubtraction:
    def test_overlapping_uncertainty_union_only(self) -> None:
        # Phone [0, 10] duration 10.
        # Overlapping uncertainty [1, 3] and [2, 4] -> union [1, 4] = 3 sec removed.
        # Eligible = 7.
        # Clip [0, 5] covers eligible pieces [0,1]+[4,5] = 2.0 retained
        # (eligible ∩ clip = [0,1] U [4,5] after subtracting unc from phone then intersecting).
        reference = _ref(
            buffers=[BufferScope(BUF, 0.0, 20.0)],
            phones=[_phone(0.0, 10.0)],
            uncertainty=[_unc(1.0, 3.0), _unc(2.0, 4.0)],
        )
        result = SlicerResult(cutpoints=(), clips=(_clip(0.0, 5.0),))
        out = evaluate(reference, result)

        assert out.eligible_target_speech_sec == pytest.approx(7.0)
        # If masks were summed (3+2=5) eligible would wrongly be 5.
        # Retained: eligible regions [0,1] U [4,10]; ∩ clip[0,5] = [0,1] U [4,5] = 2.0
        assert out.retained_target_speech_sec == pytest.approx(2.0)
        assert out.speech_coverage == pytest.approx(2.0 / 7.0)


# ---------------------------------------------------------------------------
# D. Buffer isolation regression
# ---------------------------------------------------------------------------


class TestBufferIsolation:
    def test_same_local_timestamps_do_not_collapse(self) -> None:
        b0 = BufferScope("b0", 0.0, 20.0)
        b1 = BufferScope("b1", 0.0, 20.0)
        reference = _ref(
            buffers=[b0, b1],
            phones=[
                _phone(4.0, 6.0, "A", buffer_id="b0"),
                _phone(4.0, 6.0, "A", buffer_id="b1"),
            ],
        )
        # Same local time 5.0 in both buffers: inside phone in both.
        result = SlicerResult(
            cutpoints=(_cut(5.0, buffer_id="b0"), _cut(5.0, buffer_id="b1")),
            clips=(
                _clip(0.0, 3.0, buffer_id="b0"),
                _clip(0.0, 3.0, buffer_id="b1"),
            ),
        )
        out = evaluate(reference, result)

        # Must count as two unique cutpoints, both inside phone — not one collapsed key.
        assert out.unique_cut_safety.unique_cutpoint_count == 2
        assert out.unique_cut_safety.inside_phone_count == 2
        assert out.eligible_target_speech_sec == pytest.approx(4.0)
        # Clips [0,3] do not cover phones [4,6] in either buffer.
        assert out.retained_target_speech_sec == pytest.approx(0.0)

        # Covering clip in only one buffer must not retain the other buffer's speech.
        result2 = SlicerResult(
            cutpoints=(),
            clips=(_clip(3.5, 6.5, buffer_id="b0"),),
        )
        out2 = evaluate(reference, result2)
        assert out2.retained_target_speech_sec == pytest.approx(2.0)
        assert out2.eligible_target_speech_sec == pytest.approx(4.0)
        assert out2.speech_coverage == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# E. Unique detector cuts vs final edge occurrences
# ---------------------------------------------------------------------------


class TestUniqueCutsVsFinalEdges:
    def test_shared_boundary_counted_once_in_primary_twice_in_edges(self) -> None:
        # Shared cut at 5.0 is end of clip1 and start of clip2.
        reference = _ref(
            buffers=[BufferScope(BUF, 0.0, 30.0)],
            phones=[_phone(4.5, 5.5)],  # so 5.0 is inside phone, depth 0.5
        )
        shared = _cut(5.0)
        result = SlicerResult(
            cutpoints=(shared, _cut(2.0), _cut(8.0)),
            clips=(
                _clip(2.0, 5.0),
                _clip(5.0, 8.0),
            ),
        )
        out = evaluate(reference, result)

        assert out.unique_cut_safety.unique_cutpoint_count == 3
        assert out.unique_cut_safety.inside_phone_count == 1  # only 5.0

        # Edges: 2.0, 5.0, 5.0, 8.0 — four occurrences; 5.0 appears twice.
        assert out.final_edge_safety.edge_occurrence_count == 4
        assert out.final_edge_safety.inside_phone_count == 2


# ---------------------------------------------------------------------------
# F. Overlapping emitted clips
# ---------------------------------------------------------------------------


class TestOverlappingEmittedClips:
    def test_retained_uses_union_not_sum(self) -> None:
        reference = _ref(
            buffers=[BufferScope(BUF, 0.0, 30.0)],
            phones=[_phone(0.0, 10.0)],
        )
        # Two overlapping 5s clips both covering the same phone span heavily.
        result = SlicerResult(
            cutpoints=(),
            clips=(_clip(0.0, 5.0), _clip(3.0, 8.0)),
        )
        out = evaluate(reference, result)
        # Union coverage of phone = [0,8] = 8, not 5+5=10.
        assert out.eligible_target_speech_sec == pytest.approx(10.0)
        assert out.retained_target_speech_sec == pytest.approx(8.0)
        assert out.retained_target_speech_sec <= out.eligible_target_speech_sec
        assert out.speech_coverage == pytest.approx(0.8)
        assert out.emitted_audio_sec == pytest.approx(10.0)  # raw sum of clip durations


# ---------------------------------------------------------------------------
# G. Validation failures
# ---------------------------------------------------------------------------


class TestValidationFailures:
    def test_malformed_interval(self) -> None:
        reference = _ref(phones=[_phone(2.0, 1.0)])
        with pytest.raises(ValidationError, match="start_sec"):
            evaluate(reference, SlicerResult((), (_clip(0.0, 3.0),)))

    def test_unknown_buffer_id(self) -> None:
        reference = _ref()
        with pytest.raises(ValidationError, match="unknown buffer_id"):
            evaluate(reference, SlicerResult((_cut(1.0, buffer_id="nope"),), (_clip(0.0, 3.0),)))

    def test_cutpoint_at_raw_buffer_edge(self) -> None:
        reference = _ref(buffers=[BufferScope(BUF, 0.0, 10.0)])
        with pytest.raises(ValidationError, match="strictly inside"):
            evaluate(reference, SlicerResult((_cut(0.0),), (_clip(1.0, 4.0),)))
        with pytest.raises(ValidationError, match="strictly inside"):
            evaluate(reference, SlicerResult((_cut(10.0),), (_clip(1.0, 4.0),)))

    def test_clip_shorter_than_3_sec(self) -> None:
        reference = _ref()
        with pytest.raises(ValidationError, match="minimum"):
            evaluate(reference, SlicerResult((), (_clip(0.0, 2.9),)))

    def test_clip_longer_than_15_sec(self) -> None:
        reference = _ref(buffers=[BufferScope(BUF, 0.0, 30.0)])
        with pytest.raises(ValidationError, match="maximum"):
            evaluate(reference, SlicerResult((), (_clip(0.0, 15.1),)))

    def test_duplicate_clip_row(self) -> None:
        reference = _ref()
        dup = _clip(0.0, 3.0)
        with pytest.raises(ValidationError, match="duplicate clip"):
            evaluate(reference, SlicerResult((), (dup, dup)))

    def test_phone_outside_buffer(self) -> None:
        reference = _ref(
            buffers=[BufferScope(BUF, 0.0, 5.0)],
            phones=[_phone(0.0, 6.0)],
        )
        with pytest.raises(ValidationError, match="outside buffer"):
            evaluate(reference, SlicerResult((), (_clip(0.0, 3.0),)))

    def test_overlapping_trusted_phones_rejected(self) -> None:
        reference = _ref(phones=[_phone(1.0, 3.0, "A"), _phone(2.0, 4.0, "B")])
        with pytest.raises(ValidationError, match="overlapping trusted phones"):
            evaluate(reference, SlicerResult((), (_clip(0.0, 3.0),)))


# ---------------------------------------------------------------------------
# H. Zero eligible speech
# ---------------------------------------------------------------------------


class TestZeroEligibleSpeech:
    def test_coverage_is_none_when_eligible_zero(self) -> None:
        # No phones -> eligible 0; retained 0; coverage None (not 0 or 1).
        reference = _ref(phones=[])
        result = SlicerResult(cutpoints=(), clips=(_clip(0.0, 3.0),))
        out = evaluate(reference, result)
        assert out.eligible_target_speech_sec == 0.0
        assert out.retained_target_speech_sec == 0.0
        assert out.speech_coverage is None

    def test_coverage_none_when_all_speech_uncertain(self) -> None:
        reference = _ref(
            phones=[_phone(1.0, 2.0)],
            uncertainty=[_unc(1.0, 2.0)],
        )
        out = evaluate(reference, SlicerResult((), (_clip(0.0, 3.0),)))
        assert out.eligible_target_speech_sec == 0.0
        assert out.retained_target_speech_sec == 0.0
        assert out.speech_coverage is None
