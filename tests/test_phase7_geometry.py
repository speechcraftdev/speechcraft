"""Phase 7 geometry-sweep prep: configs, fingerprints, cache guard, workers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from adapter.buckeye_loader import (
    PHASE5_COHORT_FINGERPRINT,
    PHASE7_SMOKE_SUBSET,
    LoadedRecording,
    derive_full_cohort,
)
from adapter.buckeye_validate import run_validation
from adapter.canonical_smoke import GeometryRun
from adapter.config import (
    A_O50_8,
    CURRENT_A,
    D_O0_8,
    O0_2,
    O0_4,
    O12_5_4,
    O25_4,
    O25_8,
    O50_4,
    O75_8,
    PHASE7_FINALISTS,
    PHASE7_GEOMETRIES,
    PROPER_D,
    GeometryConfig,
    aggregate_spacing_samples,
    recurrent_overlap_fraction,
    resolve_geometries,
)
from adapter.diagnostics import geometry_fingerprint
from referee import BufferScope, Clip, Cutpoint, RecordingReference, SlicerResult, evaluate
from referee.types import EvaluationResult


# Frozen from the trusted Phase-5/6 runs. Fingerprint definition is unchanged.
TRUSTED_A_GEOMETRY_FINGERPRINT = (
    "72ea8093df3fb11222b7291a784e2272246d6349ad6d5da63900e79412997498"
)
TRUSTED_D_GEOMETRY_FINGERPRINT = (
    "b35b56fbf50e01487bef7f59ef870fe839ea7b8d3f84afb9014d7c5503a3e4dc"
)


def _loaded(tmp_path: Path, speaker_id: str, recording_id: str) -> LoadedRecording:
    reference = RecordingReference(
        recording_id=recording_id,
        buffers=(BufferScope("buf0", 0.0, 30.0),),
        phones=(),
        uncertainty_intervals=(),
    )
    return LoadedRecording(
        speaker_id=speaker_id,
        recording_id=recording_id,
        audio_path=tmp_path / f"{recording_id}.wav",
        sample_rate_hz=16000,
        reference=reference,
    )


def _diag(name: str, fingerprint: str, ts_hash: str, recording_id: str):
    from adapter.diagnostics import ExecutionDiagnostics

    return ExecutionDiagnostics(
        geometry_name=name,
        geometry_canonical="{}",
        geometry_fingerprint=fingerprint,
        instance_id=abs(hash((recording_id, name))) % 100000 + 1,
        workdir=f"/tmp/{recording_id}_{name}",
        vad_cache_dir=f"/tmp/{recording_id}_{name}/vad",
        feature_cache_dir=f"/tmp/{recording_id}_{name}/feat",
        vad_cache_paths=(f"/tmp/{recording_id}_{name}/vad/x.json",),
        feature_cache_paths=(f"/tmp/{recording_id}_{name}/feat/x.json",),
        vad_observation_count=10 + abs(hash(name)) % 50,
        vad_timestamp_sha256=ts_hash,
        vad_probability_sha256="pr-" + name,
        candidate_cutpoint_sha256="cands-" + name + "-" + recording_id,
        selected_clip_sha256="clips-" + name + "-" + recording_id,
        policy_fingerprint="pol",
        selected_cutpoint_sha256="cuts-" + name + "-" + recording_id,
    )


def fake_load(speaker_id: str, recording_id: str) -> LoadedRecording:
    return _loaded(Path("/tmp"), speaker_id, recording_id)


def fake_run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
    rec = loaded.recording_id
    fingerprint = geometry_fingerprint(config)
    if config.name in {"current_A", "A_O50_8"}:
        result = SlicerResult(
            cutpoints=(Cutpoint(rec, "buf0", 4.0),),
            clips=(Clip(rec, "buf0", 2.0, 8.0),),
        )
        ts = "ts-A"
    elif config.name in {"proper_D", "D_O0_8"}:
        result = SlicerResult(cutpoints=(), clips=())
        ts = "ts-D"
    else:
        result = SlicerResult(
            cutpoints=(Cutpoint(rec, "buf0", 5.0),),
            clips=(Clip(rec, "buf0", 3.0, 9.0),),
        )
        ts = "ts-" + config.name
    return GeometryRun(
        result=result,
        diagnostics=_diag(config.name, fingerprint, ts, rec),
    )


def fake_eval(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
    return evaluate(loaded.reference, result)


class TestPhase7GeometryDefinitions:
    def test_exact_window_hop_offsets(self) -> None:
        expected = {
            "A_O50_8": (512, 256, (0, 128)),
            "D_O0_8": (512, 512, (0, 128, 256, 384)),
            "O75_8": (512, 128, (0,)),
            "O25_8": (512, 384, (0, 128, 256)),
            "O50_4": (512, 256, (0, 64, 128, 192)),
            "O25_4": (512, 384, (0, 64, 128, 192, 256, 320)),
            "O0_4": (512, 512, (0, 64, 128, 192, 256, 320, 384, 448)),
        }
        by_name = {config.name: config for config in PHASE7_GEOMETRIES}
        assert tuple(by_name) == tuple(expected)
        for name, (window, hop, offsets) in expected.items():
            config = by_name[name]
            assert config.window_samples == window
            assert config.hop_samples == hop
            assert config.offsets == offsets
            assert config.sample_rate_hz == 16000
            assert config.min_quiet_run_ms is None
            assert config.scoring is None

    def test_overlap_and_spacing(self) -> None:
        assert recurrent_overlap_fraction(A_O50_8) == pytest.approx(0.50)
        assert recurrent_overlap_fraction(D_O0_8) == pytest.approx(0.0)
        assert recurrent_overlap_fraction(O75_8) == pytest.approx(0.75)
        assert recurrent_overlap_fraction(O25_8) == pytest.approx(0.25)
        assert recurrent_overlap_fraction(O50_4) == pytest.approx(0.50)
        assert recurrent_overlap_fraction(O25_4) == pytest.approx(0.25)
        assert recurrent_overlap_fraction(O0_4) == pytest.approx(0.0)
        assert aggregate_spacing_samples(A_O50_8) == 128
        assert aggregate_spacing_samples(D_O0_8) == 128
        assert aggregate_spacing_samples(O75_8) == 128
        assert aggregate_spacing_samples(O25_8) == 128
        assert aggregate_spacing_samples(O50_4) == 64
        assert aggregate_spacing_samples(O25_4) == 64
        assert aggregate_spacing_samples(O0_4) == 64
        assert aggregate_spacing_samples(A_O50_8) / A_O50_8.sample_rate_hz == pytest.approx(0.008)
        assert aggregate_spacing_samples(O0_4) / O0_4.sample_rate_hz == pytest.approx(0.004)
        assert recurrent_overlap_fraction(O12_5_4) == pytest.approx(0.125)
        assert recurrent_overlap_fraction(O0_2) == pytest.approx(0.0)
        assert O12_5_4.window_samples == 512
        assert O12_5_4.hop_samples == 448
        assert O12_5_4.offsets == (0, 64, 128, 192, 256, 320, 384)
        assert O0_2.window_samples == 512
        assert O0_2.hop_samples == 512
        assert O0_2.offsets == tuple(range(0, 512, 32))
        assert aggregate_spacing_samples(O12_5_4) == 64
        assert aggregate_spacing_samples(O0_2) == 32
        assert aggregate_spacing_samples(O12_5_4) / O12_5_4.sample_rate_hz == pytest.approx(0.004)
        assert aggregate_spacing_samples(O0_2) / O0_2.sample_rate_hz == pytest.approx(0.002)

    def test_fingerprints_unique_and_a_d_unchanged(self) -> None:
        fingerprints = [geometry_fingerprint(config) for config in PHASE7_GEOMETRIES]
        assert len(set(fingerprints)) == 7
        extra = [geometry_fingerprint(O12_5_4), geometry_fingerprint(O0_2)]
        assert extra[0] not in fingerprints
        assert extra[1] not in fingerprints
        assert extra[0] != extra[1]
        finalist_fps = [geometry_fingerprint(config) for config in PHASE7_FINALISTS]
        assert len(set(finalist_fps)) == 6
        assert tuple(config.name for config in PHASE7_FINALISTS) == (
            "A_O50_8",
            "D_O0_8",
            "O25_4",
            "O0_4",
            "O12.5_4",
            "O0_2",
        )
        assert geometry_fingerprint(CURRENT_A) == TRUSTED_A_GEOMETRY_FINGERPRINT
        assert geometry_fingerprint(PROPER_D) == TRUSTED_D_GEOMETRY_FINGERPRINT
        assert geometry_fingerprint(A_O50_8) == geometry_fingerprint(CURRENT_A)
        assert geometry_fingerprint(D_O0_8) == geometry_fingerprint(PROPER_D)
        assert geometry_fingerprint(A_O50_8) != geometry_fingerprint(D_O0_8)

    def test_offsets_are_strictly_inside_hop(self) -> None:
        for config in (*PHASE7_GEOMETRIES, O12_5_4, O0_2):
            assert all(0 <= offset < config.hop_samples for offset in config.offsets)

    def test_resolve_geometries_subset(self) -> None:
        resolved = resolve_geometries("A_O50_8,D_O0_8,O25_4,O0_4,O12.5_4,O0_2")
        assert tuple(config.name for config in resolved) == (
            "A_O50_8",
            "D_O0_8",
            "O25_4",
            "O0_4",
            "O12.5_4",
            "O0_2",
        )
        with pytest.raises(RuntimeError, match="unknown geometry"):
            resolve_geometries("OpenVPI")


class TestSmokeSubset:
    def test_smoke_subset_is_frozen_and_in_cohort(self) -> None:
        assert len(PHASE7_SMOKE_SUBSET) == 10
        speakers = [row[0] for row in PHASE7_SMOKE_SUBSET]
        assert len(set(speakers)) == 10
        assert ("s03", "s0301a") in PHASE7_SMOKE_SUBSET
        assert PHASE7_SMOKE_SUBSET[:4] == (
            ("s01", "s0101b"),
            ("s02", "s0201b"),
            ("s03", "s0301a"),
            ("s04", "s0401b"),
        )
        cohort = derive_full_cohort()
        assert cohort.fingerprint == PHASE5_COHORT_FINGERPRINT
        accepted = set(cohort.recordings)
        assert set(PHASE7_SMOKE_SUBSET) <= accepted


class TestWorkersDeterminism:
    def test_workers_1_and_2_same_pooled_metrics(self, tmp_path: Path) -> None:
        subset = (("s01", "s0101b"), ("s02", "s0201b"))
        kwargs = dict(
            subset=subset,
            load_fn=fake_load,
            run_fn=fake_run,
            eval_fn=fake_eval,
            contenders=(A_O50_8, D_O0_8),
            spot_check_ids=(subset[0],),
            resume=False,
        )
        one = run_validation(output_dir=tmp_path / "w1", workers=1, **kwargs)
        two = run_validation(output_dir=tmp_path / "w2", workers=2, **kwargs)
        assert one["pooled"] == two["pooled"]
        assert one["recording_count"] == two["recording_count"] == 2
        assert one["contender_names"] == ["A_O50_8", "D_O0_8"]
        assert two["geometry_table"]
        assert two["geometry_delta_vs_A"]
        assert one["workers"] == 1
        assert two["workers"] == 2


class TestGeometryCollapseGuard:
    def test_identical_vad_observations_abort(self, tmp_path: Path) -> None:
        def run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
            base = fake_run(loaded, config)
            return GeometryRun(
                result=base.result,
                diagnostics=replace(
                    base.diagnostics,
                    vad_observation_count=7,
                    vad_timestamp_sha256="same-ts",
                    vad_probability_sha256="same-pr",
                ),
            )

        with pytest.raises(RuntimeError, match="collapsed onto identical VAD"):
            run_validation(
                output_dir=tmp_path / "out",
                subset=(("s01", "s0101b"),),
                load_fn=fake_load,
                run_fn=run,
                eval_fn=fake_eval,
                contenders=(A_O50_8, O25_8),
                spot_check_ids=(("s01", "s0101b"),),
            )
        assert not (tmp_path / "out" / "summary.json").exists()

    def test_diagnostics_fingerprint_must_match_config(self, tmp_path: Path) -> None:
        def run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
            base = fake_run(loaded, config)
            return GeometryRun(
                result=base.result,
                diagnostics=replace(base.diagnostics, geometry_fingerprint="not-the-config"),
            )

        with pytest.raises(RuntimeError, match="diagnostics fingerprint"):
            run_validation(
                output_dir=tmp_path / "out",
                subset=(("s01", "s0101b"),),
                load_fn=fake_load,
                run_fn=run,
                eval_fn=fake_eval,
                contenders=(A_O50_8, D_O0_8),
                spot_check_ids=(("s01", "s0101b"),),
            )
