"""Tiny helpers for the real canonical A/D smoke (not a benchmark framework)."""

from __future__ import annotations

import wave
from dataclasses import dataclass, fields
from pathlib import Path

from adapter.config import CURRENT_A, PROPER_D, GeometryConfig
from adapter.canonical_executor import execute_canonical_diagnosed
from adapter.convert import to_slicer_result
from adapter.diagnostics import (
    ExecutionDiagnostics,
    geometry_sensitive_signals_differ,
    same_geometry_signatures,
    write_tiny_mono_wav,
)
from adapter.types import SlicerRequest
from referee import BufferScope, RecordingReference, SlicerResult, evaluate
from referee.validate import validate

SMOKE_RECORDING_ID = "smoke0"
SMOKE_BUFFER_ID = "buf0"


@dataclass(frozen=True)
class GeometryRun:
    result: SlicerResult
    diagnostics: ExecutionDiagnostics


def _audio_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        rate = reader.getframerate()
        return reader.getnframes() / rate if rate else 0.0


def run_one(audio_path: Path, config: GeometryConfig) -> GeometryRun:
    duration = _audio_duration_sec(audio_path)
    request = SlicerRequest(
        recording_id=SMOKE_RECORDING_ID,
        audio_path=audio_path,
        sample_rate_hz=config.sample_rate_hz,
        buffers=(BufferScope(SMOKE_BUFFER_ID, 0.0, float(duration)),),
        config=config,
    )
    assert {f.name for f in fields(request)} == {
        "recording_id",
        "audio_path",
        "sample_rate_hz",
        "buffers",
        "config",
    }
    execution = execute_canonical_diagnosed(request)
    result = to_slicer_result(execution.raw)
    assert {f.name for f in fields(result)} == {"cutpoints", "clips"}
    return GeometryRun(result=result, diagnostics=execution.diagnostics)


def evaluator_reference_for(audio_path: Path) -> RecordingReference:
    duration = _audio_duration_sec(audio_path)
    return RecordingReference(
        recording_id=SMOKE_RECORDING_ID,
        buffers=(BufferScope(SMOKE_BUFFER_ID, 0.0, float(duration)),),
        phones=(),
        uncertainty_intervals=(),
    )


def assert_valid_public_result(audio_path: Path, result: SlicerResult) -> None:
    reference = evaluator_reference_for(audio_path)
    validate(reference, result)
    scored = evaluate(reference, result)
    assert scored.clip_count == len(result.clips)


def assert_independent_state(left: ExecutionDiagnostics, right: ExecutionDiagnostics) -> None:
    assert left.instance_id != right.instance_id
    assert left.workdir != right.workdir
    assert left.vad_cache_dir != right.vad_cache_dir
    assert left.feature_cache_dir != right.feature_cache_dir
    left_paths = set(left.vad_cache_paths) | set(left.feature_cache_paths)
    right_paths = set(right.vad_cache_paths) | set(right.feature_cache_paths)
    overlap = left_paths & right_paths
    assert not overlap, f"A/D shared cache artifacts: {sorted(overlap)}"


def assert_a_d_not_collapsed(a: ExecutionDiagnostics, d: ExecutionDiagnostics) -> None:
    assert a.geometry_fingerprint != d.geometry_fingerprint, (
        "A and D geometry fingerprints must differ"
    )
    assert_independent_state(a, d)
    if not geometry_sensitive_signals_differ(a, d):
        raise AssertionError(
            "A and D geometry-sensitive observation signatures are identical "
            f"(count={a.vad_observation_count}, "
            f"ts={a.vad_timestamp_sha256}, "
            f"prob={a.vad_probability_sha256}). "
            "The two geometries collapsed onto shared feature computation."
        )


def run_ad_order_smoke(audio_path: Path) -> dict[str, GeometryRun]:
    """Run A→D, then separately D→A, on the same fixture."""
    a_in_ad = run_one(audio_path, CURRENT_A)
    d_in_ad = run_one(audio_path, PROPER_D)
    d_in_da = run_one(audio_path, PROPER_D)
    a_in_da = run_one(audio_path, CURRENT_A)
    return {
        "A_in_AD": a_in_ad,
        "D_in_AD": d_in_ad,
        "D_in_DA": d_in_da,
        "A_in_DA": a_in_da,
    }


def assert_order_independence(runs: dict[str, GeometryRun]) -> None:
    a_ad = runs["A_in_AD"].diagnostics
    a_da = runs["A_in_DA"].diagnostics
    d_ad = runs["D_in_AD"].diagnostics
    d_da = runs["D_in_DA"].diagnostics
    if not same_geometry_signatures(a_ad, a_da):
        raise AssertionError(
            "Geometry A signatures changed across A→D vs D→A execution order "
            f"(AD fp={a_ad.geometry_fingerprint} count={a_ad.vad_observation_count} "
            f"ts={a_ad.vad_timestamp_sha256} vs "
            f"DA fp={a_da.geometry_fingerprint} count={a_da.vad_observation_count} "
            f"ts={a_da.vad_timestamp_sha256})"
        )
    if not same_geometry_signatures(d_ad, d_da):
        raise AssertionError(
            "Geometry D signatures changed across A→D vs D→A execution order "
            f"(AD fp={d_ad.geometry_fingerprint} count={d_ad.vad_observation_count} "
            f"ts={d_ad.vad_timestamp_sha256} vs "
            f"DA fp={d_da.geometry_fingerprint} count={d_da.vad_observation_count} "
            f"ts={d_da.vad_timestamp_sha256})"
        )


def compact_summary(runs: dict[str, GeometryRun]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for label, run in runs.items():
        diag = run.diagnostics
        result = run.result
        out[label] = {
            "geometry_name": diag.geometry_name,
            "geometry_canonical": diag.geometry_canonical,
            "geometry_fingerprint": diag.geometry_fingerprint,
            "instance_id": diag.instance_id,
            "workdir": diag.workdir,
            "vad_observation_count": diag.vad_observation_count,
            "vad_timestamp_sha256": diag.vad_timestamp_sha256,
            "vad_probability_sha256": diag.vad_probability_sha256,
            "candidate_cutpoint_sha256": diag.candidate_cutpoint_sha256,
            "selected_clip_sha256": diag.selected_clip_sha256,
            "cutpoint_count": len(result.cutpoints),
            "clip_count": len(result.clips),
        }
    return out


def prepare_fixture_wav(directory: Path) -> Path:
    return write_tiny_mono_wav(directory / "canonical_smoke_16k_mono.wav")
