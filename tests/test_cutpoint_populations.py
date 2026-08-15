"""Phase 5.1: public cutpoints are selected-schedule, not the detector candidate pool.

Does not change Phase-1 evaluator semantics.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapter.canonical_executor import cutpoints_used_by_selected_schedule
from adapter.convert import to_slicer_result
from adapter.types import RawClip, RawCutpoint, RawSlicerOutput
from referee import BufferScope, Clip, Cutpoint, RecordingReference, SlicerResult, evaluate


REC = "rec0"
BUF = "buf0"


def _pool_cut(cut_id: str, time_sec: float) -> SimpleNamespace:
    return SimpleNamespace(
        cutpoint_id=cut_id,
        recording_id=REC,
        buffer_id=BUF,
        time_sec=time_sec,
    )


def _candidate(start_id: str, end_id: str) -> SimpleNamespace:
    return SimpleNamespace(start_cutpoint_id=start_id, end_cutpoint_id=end_id)


def _reference() -> RecordingReference:
    return RecordingReference(
        recording_id=REC,
        buffers=(BufferScope(BUF, 0.0, 30.0),),
        phones=(),
        uncertainty_intervals=(),
    )


class TestSelectedSchedulePopulation:
    def test_public_cutpoints_are_schedule_endpoints_not_candidate_pool(self) -> None:
        pool = (
            _pool_cut("c1", 1.0),
            _pool_cut("c2", 2.0),
            _pool_cut("c3", 4.0),
            _pool_cut("c4", 8.0),
            _pool_cut("c5", 12.0),
            _pool_cut("c6", 16.0),
            _pool_cut("c7", 20.0),
        )
        selected = (
            _candidate("c2", "c4"),
            _candidate("c4", "c6"),
        )
        used = cutpoints_used_by_selected_schedule(pool, selected)
        used_ids = [cut.cutpoint_id for cut in used]
        assert used_ids == ["c2", "c4", "c6"]
        assert len(used) < len(pool)
        unused = {cut.cutpoint_id for cut in pool} - set(used_ids)
        assert unused == {"c1", "c3", "c5", "c7"}

    def test_missing_schedule_id_fails_loudly(self) -> None:
        pool = (_pool_cut("c1", 2.0), _pool_cut("c2", 8.0))
        selected = (_candidate("c1", "missing"),)
        with pytest.raises(RuntimeError, match="missing from the detector pool"):
            cutpoints_used_by_selected_schedule(pool, selected)

    def test_three_populations_are_distinct_under_unchanged_evaluator(self) -> None:
        pool_times = (2.0, 4.0, 8.0, 10.0, 14.0)
        selected_times = (2.0, 8.0, 14.0)
        clips = (
            Clip(REC, BUF, 2.0, 8.0),
            Clip(REC, BUF, 8.0, 14.0),
        )
        candidate_result = SlicerResult(
            cutpoints=tuple(Cutpoint(REC, BUF, t) for t in pool_times),
            clips=clips,
        )
        selected_result = SlicerResult(
            cutpoints=tuple(Cutpoint(REC, BUF, t) for t in selected_times),
            clips=clips,
        )
        reference = _reference()
        candidate_score = evaluate(reference, candidate_result)
        selected_score = evaluate(reference, selected_result)

        assert candidate_score.unique_cut_safety.unique_cutpoint_count == 5
        assert selected_score.unique_cut_safety.unique_cutpoint_count == 3
        assert selected_score.final_edge_safety.edge_occurrence_count == 4
        assert selected_score.clip_count == 2
        assert (
            selected_score.final_edge_safety.edge_occurrence_count
            == 2 * selected_score.clip_count
        )
        assert (
            candidate_score.unique_cut_safety.unique_cutpoint_count
            != selected_score.unique_cut_safety.unique_cutpoint_count
        )
        assert (
            selected_score.unique_cut_safety.unique_cutpoint_count
            != selected_score.final_edge_safety.edge_occurrence_count
        )
        assert candidate_score.emitted_audio_sec == selected_score.emitted_audio_sec
        assert candidate_score.clip_count == selected_score.clip_count

    def test_adapter_conversion_preserves_selected_not_pool(self) -> None:
        pool = (
            _pool_cut("c1", 1.0),
            _pool_cut("c2", 2.0),
            _pool_cut("c3", 8.0),
            _pool_cut("c4", 14.0),
        )
        selected = (_candidate("c2", "c3"), _candidate("c3", "c4"))
        used = cutpoints_used_by_selected_schedule(pool, selected)
        raw = RawSlicerOutput(
            cutpoints=tuple(
                RawCutpoint(cut.recording_id, cut.buffer_id, cut.time_sec) for cut in used
            ),
            clips=(
                RawClip(REC, BUF, 2.0, 8.0),
                RawClip(REC, BUF, 8.0, 14.0),
            ),
        )
        result = to_slicer_result(raw)
        assert [cut.time_sec for cut in result.cutpoints] == [2.0, 8.0, 14.0]
        assert len(result.cutpoints) == 3
        assert len(result.clips) == 2
        score = evaluate(_reference(), result)
        assert score.unique_cut_safety.unique_cutpoint_count == 3
        assert score.final_edge_safety.edge_occurrence_count == 4
