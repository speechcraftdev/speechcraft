"""Pack frozen O0_4 detector cuts with original scores or OOF P(bad)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from adapter.canonical_executor import pack_scored_cutpoints
from adapter.config import O0_4, O0_4_LOGISTIC
from adapter.convert import to_slicer_result
from adapter.logistic_policy import apply_precomputed_p_bad
from referee.types import SlicerResult


def cuts_from_rows(rows: Sequence[dict[str, object]]) -> list[Any]:
    cuts: list[Any] = []
    for row in rows:
        cuts.append(
            SimpleNamespace(
                cutpoint_id=str(row["cutpoint_id"]),
                recording_id=str(row["recording_id"]),
                buffer_id=str(row["buffer_id"]),
                time_sec=float(row["time_sec"]),
                score=float(row["original_score"]),
                interval_start_sec=float(row["interval_start_sec"]),
                interval_end_sec=float(row["interval_end_sec"]),
                detector_name=str(row["detector_name"]),
                source_id=str(row["source_id"]),
                metadata={"original_score": float(row["original_score"])},
            )
        )
    return cuts


def pack_o0_4_baseline(rows: Sequence[dict[str, object]]) -> SlicerResult:
    return to_slicer_result(pack_scored_cutpoints(cuts_from_rows(rows), O0_4))


def pack_oof_model(
    rows: Sequence[dict[str, object]],
    p_bad_by_candidate: Mapping[str, float],
    *,
    model_id: str,
) -> SlicerResult:
    p_bad_by_cut = {
        str(row["cutpoint_id"]): float(p_bad_by_candidate[str(row["candidate_id"])])
        for row in rows
    }
    scored = apply_precomputed_p_bad(
        cuts_from_rows(rows),
        p_bad_by_cut,
        score_scale=1.0,
        model_id=model_id,
    )
    config = replace(O0_4_LOGISTIC, name=str(model_id))
    return to_slicer_result(pack_scored_cutpoints(scored, config))
