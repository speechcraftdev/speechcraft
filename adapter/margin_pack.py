"""Pack frozen O0_4 detector cuts with keep-fraction or OOF signed-margin scores."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace

from adapter.canonical_executor import pack_scored_cutpoints
from adapter.config import O0_4, O0_4_MARGIN
from adapter.convert import to_slicer_result
from adapter.logistic_pack import cuts_from_rows, pack_o0_4_baseline
from adapter.margin_labels import MARGIN_CAP_MS, MARGIN_MODEL_ID
from adapter.margin_policy import apply_precomputed_margin
from referee.types import SlicerResult


def keep_frac_name(frac: float) -> str:
    pct = int(round(float(frac) * 100.0))
    return f"O0_4_keep{pct}"


def score_weight_name(weight: float) -> str:
    if float(weight) == int(weight):
        return f"O0_4_sw{int(weight)}"
    text = f"{float(weight):g}".replace(".", "p")
    return f"O0_4_sw{text}"


def score_scale_name(scale: float) -> str:
    if float(scale) == int(scale):
        return f"{MARGIN_MODEL_ID}_s{int(scale)}"
    text = f"{float(scale):g}".replace(".", "p")
    return f"{MARGIN_MODEL_ID}_s{text}"


def keep_top_score_fraction(
    rows: Sequence[dict[str, object]],
    frac: float,
) -> list[dict[str, object]]:
    """Per-buffer candidate pruning by original_score. Not an O0_4 frontier.

    A diagnostic only: deleting candidates is not the same as sweeping how
    strongly the existing detector score influences packer selection.
    """
    if not math.isfinite(float(frac)) or float(frac) <= 0.0:
        raise RuntimeError(f"keep fraction must be positive, got {frac}")
    if float(frac) >= 1.0:
        return list(rows)
    groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]), str(row["buffer_id"]))
        groups[key].append(row)
    kept: list[dict[str, object]] = []
    for key in sorted(groups):
        group = groups[key]
        n = len(group)
        k = min(n, max(1, int(math.ceil(n * float(frac)))))
        ranked = sorted(
            group,
            key=lambda row: (-float(row["original_score"]), str(row["cutpoint_id"])),
        )
        kept.extend(ranked[:k])
    return kept


def pack_o0_4_keep(rows: Sequence[dict[str, object]], frac: float) -> SlicerResult:
    return pack_o0_4_baseline(keep_top_score_fraction(rows, frac))


def pack_o0_4_score_weight(rows: Sequence[dict[str, object]], score_weight: float) -> SlicerResult:
    """Full candidate set. Only the detector-score contribution to packer weight changes.

    score_weight=0 is frozen O0_4 (duration-only clip weights).
    """
    if not math.isfinite(float(score_weight)):
        raise RuntimeError(f"non-finite score_weight: {score_weight}")
    if float(score_weight) == 0.0:
        return pack_o0_4_baseline(rows)
    name = score_weight_name(score_weight)
    config = replace(
        O0_4,
        name=name,
        scoring="o0_4_score_weight",
        score_weight=float(score_weight),
    )
    if config.window_samples != O0_4.window_samples or config.hop_samples != O0_4.hop_samples:
        raise RuntimeError("score-weight config drifted from frozen O0_4 geometry")
    return to_slicer_result(pack_scored_cutpoints(cuts_from_rows(rows), config))


def pack_oof_margin(
    rows: Sequence[dict[str, object]],
    margin_by_candidate: Mapping[str, float],
    *,
    score_scale: float,
    model_id: str,
) -> SlicerResult:
    margin_by_cut = {
        str(row["cutpoint_id"]): float(margin_by_candidate[str(row["candidate_id"])])
        for row in rows
    }
    scored = apply_precomputed_margin(
        cuts_from_rows(rows),
        margin_by_cut,
        score_scale=score_scale,
        model_id=model_id,
        cap_ms=MARGIN_CAP_MS,
    )
    config = replace(O0_4_MARGIN, name=str(model_id))
    if config.scoring != "margin_regression":
        raise RuntimeError("O0_4_MARGIN must keep scoring=margin_regression")
    if config.window_samples != O0_4.window_samples or config.hop_samples != O0_4.hop_samples:
        raise RuntimeError("O0_4_MARGIN drifted from frozen O0_4 geometry")
    return to_slicer_result(pack_scored_cutpoints(scored, config))
