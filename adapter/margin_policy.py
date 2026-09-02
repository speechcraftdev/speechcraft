"""Apply OOF signed-margin scoring to detector cutpoints.

Does not add or drop candidates. Higher predicted margin must raise packer preference.
"""

from __future__ import annotations

from dataclasses import is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from adapter.margin_labels import MARGIN_CAP_MS
from adapter.margin_model import preference_delta


def _replace_obj(obj: Any, **changes: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return replace(obj, **changes)
    payload = dict(vars(obj))
    payload.update(changes)
    return SimpleNamespace(**payload)


def apply_precomputed_margin(
    cuts: list[Any],
    margin_by_id: dict[str, float],
    *,
    score_scale: float = 1.0,
    model_id: str = "oof",
    cap_ms: float = MARGIN_CAP_MS,
) -> list[Any]:
    """Attach OOF signed margin. Higher margin must raise packer preference."""
    updated: list[Any] = []
    for cut in cuts:
        cut_id = str(cut.cutpoint_id)
        if cut_id not in margin_by_id:
            raise RuntimeError(f"missing OOF margin for cutpoint {cut_id}")
        margin_ms = float(margin_by_id[cut_id])
        original = float(cut.score)
        delta = preference_delta(margin_ms, score_scale=score_scale, cap_ms=cap_ms)
        metadata = dict(getattr(cut, "metadata", None) or {})
        metadata["original_score"] = original
        metadata["predicted_margin_ms"] = round(margin_ms, 6)
        metadata["score_delta"] = round(delta, 6)
        metadata["margin_model_id"] = str(model_id)
        metadata["margin_score_scale"] = float(score_scale)
        metadata["policy_reasons"] = "margin_regression"
        updated.append(
            _replace_obj(
                cut,
                score=round(original + delta, 6),
                metadata=metadata,
            )
        )
    return updated
