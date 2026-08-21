"""Apply O25_4 RMS veto/penalty policies to CURRENT detector candidates."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from adapter.config import GeometryConfig, RmsPolicySpec
from adapter.feature_bundle import FeatureBundle, buffer_bounds, require_bundle_geometry
from adapter.logistic_features import LOGISTIC_FINE_RMS_SPEC
from adapter.rms_evidence import (
    FineRmsGrid,
    build_fine_rms_grid,
    evidence_depth_db,
    half_depth_width_ms,
    recording_silence_threshold,
    recording_speech_ref,
    require_grid_spec,
    short_shallow_penalty,
    silence_ratio,
)

EXPECTED_RMS_KIND = {
    "O25_4_CURRENT": "current",
    "O25_4_WEAK_VALLEY_VETO": "weak_valley_veto",
    "O25_4_SHORT_SHALLOW_PENALTY": "short_shallow_penalty",
    "O25_4_WAVEFORM_SILENCE_RATIO": "waveform_silence_ratio",
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


def family_fine_rms_spec(configs: list[GeometryConfig]) -> RmsPolicySpec | None:
    """Shared 10/8 ms grid spec for an O25_4 family, or None if nothing needs it."""
    specs = [
        config.rms_policy
        for config in configs
        if config.rms_policy is not None and config.rms_policy.kind != "current"
    ]
    if any(
        config.scoring == "logistic_boundary"
        and config.logistic is not None
        and config.logistic.feature_subset in {"all", "rms_waveform"}
        for config in configs
    ):
        specs.append(LOGISTIC_FINE_RMS_SPEC)
    if not specs:
        return None
    windows = {(spec.fine_window_ms, spec.fine_hop_ms) for spec in specs}
    if len(windows) != 1:
        raise RuntimeError(f"mixed fine RMS grids: {windows}")
    return specs[0]


def apply_rms_policy(
    cuts: list[Any],
    bundle: FeatureBundle,
    config: GeometryConfig,
    fine_grid: FineRmsGrid | None = None,
) -> list[Any]:
    """Veto or rescore CURRENT candidates from fine-scale waveform RMS.

    Never qualifies new regions. Does not read evaluator annotations.
    Local evidence is clipped to the candidate's allowed buffer. Recording
    percentiles use the union of allowed-buffer hops only.
    """
    require_bundle_geometry(bundle, config)
    spec = _spec(config)
    if spec.kind == "current":
        return list(cuts)
    grid = fine_grid if fine_grid is not None else build_fine_rms_grid(bundle, spec)
    require_grid_spec(grid, spec)
    times = grid.times_sec
    rms = grid.rms_dbfs
    speech_ref = recording_speech_ref(rms, spec) if rms else -12.0
    threshold = (
        recording_silence_threshold(rms, spec)
        if spec.kind == "waveform_silence_ratio" and rms
        else -120.0
    )
    updated: list[Any] = []
    for cut in cuts:
        t_sec = float(cut.time_sec)
        bound_start, bound_end = buffer_bounds(bundle, str(cut.buffer_id))
        depth = evidence_depth_db(
            times,
            rms,
            t_sec,
            spec,
            speech_ref,
            bound_start=bound_start,
            bound_end=bound_end,
        )
        if spec.kind == "weak_valley_veto":
            if depth is not None and depth < spec.veto_depth_db:
                continue
            updated.append(
                _copy_cut(
                    cut,
                    metadata={
                        "rms_policy": spec.kind,
                        "local_depth_db": None if depth is None else round(depth, 6),
                    },
                )
            )
        elif spec.kind == "short_shallow_penalty":
            width_ms = half_depth_width_ms(
                times,
                rms,
                t_sec,
                spec,
                speech_ref,
                bound_start=bound_start,
                bound_end=bound_end,
            )
            penalty = short_shallow_penalty(width_ms, depth, spec)
            score_delta = -penalty
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "local_depth_db": None if depth is None else round(depth, 6),
                        "valley_width_ms": round(width_ms, 6),
                    },
                )
            )
        elif spec.kind == "waveform_silence_ratio":
            ratio = silence_ratio(
                times,
                rms,
                t_sec,
                spec,
                bound_start=bound_start,
                bound_end=bound_end,
                threshold_db=threshold,
            )
            score_delta = spec.score_scale * (2.0 * ratio - 1.0)
            updated.append(
                _copy_cut(
                    cut,
                    score=round(float(cut.score) + score_delta, 6),
                    metadata={
                        "rms_policy": spec.kind,
                        "score_delta": round(score_delta, 6),
                        "silence_ratio": round(ratio, 6),
                    },
                )
            )
        else:
            raise RuntimeError(f"unknown rms_policy.kind {spec.kind!r} on {config.name}")
    return updated
