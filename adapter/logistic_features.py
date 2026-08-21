"""Frozen O0_4 boundary features for logistic scoring.

Uses the same local quiet/RMS/prominence evidence already on detector
cutpoints. Does not read evaluator annotations or Buckeye labels.
"""

from __future__ import annotations

from typing import Any

from adapter.config import LOGISTIC_BOUNDARY_FEATURE_NAMES


# Same caps as quiet_evidence_score. Frozen; not learned.
QUIET_RUN_CAP_MS = 250.0
QUIET_SIDE_CAP_MS = 120.0
QUIET_SIDE_DENOM_MS = 240.0
RMS_QUIET_DENOM_DB = 100.0
PROMINENCE_DENOM_DB = 20.0


def cut_quiet_run_ms(cut: Any) -> float:
    metadata = getattr(cut, "metadata", None) or {}
    return float(
        metadata.get("run_duration_ms")
        or max(0.0, (float(cut.interval_end_sec) - float(cut.interval_start_sec)) * 1000.0)
    )


def cut_quiet_before_ms(cut: Any) -> float:
    return max(0.0, (float(cut.time_sec) - float(cut.interval_start_sec)) * 1000.0)


def cut_quiet_after_ms(cut: Any) -> float:
    return max(0.0, (float(cut.interval_end_sec) - float(cut.time_sec)) * 1000.0)


def cut_rms_dbfs(cut: Any) -> float:
    rms = getattr(cut, "rms_min_dbfs", None)
    return float(rms if rms is not None else -60.0)


def cut_prominence_db(cut: Any) -> float:
    metadata = getattr(cut, "metadata", None) or {}
    return float(metadata.get("score_max", 0.0) or 0.0)


def scale_boundary_features(
    *,
    quiet_run_ms: float,
    quiet_before_ms: float,
    quiet_after_ms: float,
    rms_dbfs: float,
    local_prominence_db: float,
) -> tuple[float, ...]:
    """Map raw evidence onto a frozen 5-d unit-ish vector."""
    return (
        min(float(quiet_run_ms), QUIET_RUN_CAP_MS) / QUIET_RUN_CAP_MS,
        min(float(quiet_before_ms), QUIET_SIDE_CAP_MS) / QUIET_SIDE_DENOM_MS,
        min(float(quiet_after_ms), QUIET_SIDE_CAP_MS) / QUIET_SIDE_DENOM_MS,
        max(0.0, -float(rms_dbfs)) / RMS_QUIET_DENOM_DB,
        max(0.0, float(local_prominence_db)) / PROMINENCE_DENOM_DB,
    )


def features_from_cut(cut: Any) -> tuple[float, ...]:
    """Extract the frozen logistic vector from a detector cutpoint."""
    names = LOGISTIC_BOUNDARY_FEATURE_NAMES
    if names != (
        "quiet_run_norm",
        "quiet_before_norm",
        "quiet_after_norm",
        "rms_quiet_norm",
        "prominence_norm",
    ):
        raise RuntimeError(f"unexpected logistic feature names: {names}")
    return scale_boundary_features(
        quiet_run_ms=cut_quiet_run_ms(cut),
        quiet_before_ms=cut_quiet_before_ms(cut),
        quiet_after_ms=cut_quiet_after_ms(cut),
        rms_dbfs=cut_rms_dbfs(cut),
        local_prominence_db=cut_prominence_db(cut),
    )
