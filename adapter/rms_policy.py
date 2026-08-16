"""Apply Phase-8 RMS/evidence policies to CURRENT detector candidates."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from adapter.config import GeometryConfig, RmsPolicySpec
from adapter.feature_bundle import (
    FeatureBundle,
    as_float_tuple,
    buffer_bounds,
    require_bundle_geometry,
)
from adapter.rms_evidence import (
    argmin_in_bounds,
    bilateral_score,
    clip_interval,
    multiscale_pause_score,
    placement_grid,
    prominence_drops_db,
    prominence_score,
    valley_shape_score,
)

EXPECTED_RMS_KIND = {
    "O25_4_CURRENT": "current",
    "O25_4_RMS_PROMINENCE": "prominence",
    "O25_4_MULTISCALE_RMS": "multiscale",
    "O25_4_VALLEY_WIDTH_DEPTH": "valley",
    "O25_4_BILATERAL_CONTRAST": "bilateral",
    "O25_4_RMS_MIN_PLACEMENT": "min_placement",
    "O25_4_MULTISCALE_MIN": "multiscale_min",
}


def _spec(config: GeometryConfig) -> RmsPolicySpec:
    if config.rms_policy is None:
        raise RuntimeError(
            f"{config.name} has no rms_policy; refusing silent CURRENT fallback"
        )
    expected = EXPECTED_RMS_KIND.get(config.name)
    if expected is not None and config.rms_policy.kind != expected:
        raise RuntimeError(
            f"{config.name} rms_policy.kind={config.rms_policy.kind!r} != {expected!r}"
        )
    return config.rms_policy


def _copy_cut(cut: Any, **changes: Any) -> Any:
    metadata = dict(getattr(cut, "metadata", None) or {})
    if "metadata" in changes:
        extra = dict(changes.pop("metadata"))
        metadata.update(extra)
    return replace(cut, metadata=metadata, **changes)


def apply_rms_policy(cuts: list[Any], bundle: FeatureBundle, config: GeometryConfig) -> list[Any]:
    """Rescore / re-place CURRENT candidates. Never qualifies new regions."""
    require_bundle_geometry(bundle, config)
    spec = _spec(config)
    if spec.kind == "current":
        return list(cuts)
    times = as_float_tuple(bundle.vad_centers_sec)
    rms = as_float_tuple(bundle.frame_rms_dbfs)
    updated: list[Any] = []
    for cut in cuts:
        if spec.kind == "prominence":
            left, right = prominence_drops_db(times, rms, float(cut.time_sec), spec)
            evidence = prominence_score(left, right)
            score_delta = spec.score_scale * evidence
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "left_drop_db": round(left, 6),
                        "right_drop_db": round(right, 6),
                    },
                )
            )
        elif spec.kind == "multiscale":
            evidence = multiscale_pause_score(times, rms, float(cut.time_sec), spec)
            score_delta = spec.score_scale * evidence
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "multiscale_score": round(evidence, 6),
                    },
                )
            )
        elif spec.kind == "valley":
            evidence = valley_shape_score(times, rms, float(cut.time_sec), spec)
            score_delta = spec.score_scale * evidence
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "valley_score": round(evidence, 6),
                    },
                )
            )
        elif spec.kind == "bilateral":
            left, right = prominence_drops_db(times, rms, float(cut.time_sec), spec)
            evidence = bilateral_score(left, right)
            score_delta = spec.score_scale * evidence
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "left_drop_db": round(left, 6),
                        "right_drop_db": round(right, 6),
                    },
                )
            )
        elif spec.kind == "min_placement":
            updated.append(_place_at_rms_min(cut, bundle, spec, score_delta=0.0))
        elif spec.kind == "multiscale_min":
            evidence = multiscale_pause_score(times, rms, float(cut.time_sec), spec)
            placed = _place_at_rms_min(
                cut, bundle, spec, score_delta=spec.score_scale * evidence
            )
            updated.append(placed)
        else:
            raise RuntimeError(f"unknown rms_policy.kind {spec.kind!r} on {config.name}")
    return updated


def _place_at_rms_min(
    cut: Any,
    bundle: FeatureBundle,
    spec: RmsPolicySpec,
    *,
    score_delta: float,
) -> Any:
    bound_start, bound_end = buffer_bounds(bundle, str(cut.buffer_id))
    region_start, region_end = clip_interval(
        float(cut.interval_start_sec),
        float(cut.interval_end_sec),
        bound_start,
        bound_end,
    )
    grid_t, grid_rms = placement_grid(
        audio=bundle.audio,
        sample_rate_hz=int(bundle.sample_rate_hz),
        start_sec=region_start,
        end_sec=region_end,
        window_ms=spec.placement_window_ms,
        hop_ms=spec.placement_hop_ms,
    )
    placed = argmin_in_bounds(grid_t, grid_rms, region_start, region_end)
    if placed is None:
        placed = float(cut.time_sec)
    placed = min(max(placed, region_start), region_end)
    return _copy_cut(
        cut,
        time_sec=round(float(placed), 6),
        score=round(float(cut.score) + score_delta, 6),
        metadata={
            "rms_policy": spec.kind,
            "score_delta": round(float(score_delta), 6),
            "original_time_sec": float(cut.time_sec),
            "placed_time_sec": round(float(placed), 6),
        },
    )
