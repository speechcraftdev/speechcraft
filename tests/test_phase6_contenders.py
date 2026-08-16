"""Phase 6: two A-derived contenders plus four-way aggregation tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapter.buckeye_loader import PHASE5_COHORT_FINGERPRINT, LoadedRecording, derive_full_cohort
from adapter.buckeye_metrics import (
    delta_vs_a_table,
    four_way_comparison_table,
    interpret_pareto,
    metrics_from_evaluation,
    paired_recording_comparison,
)
from adapter.buckeye_validate import completed_recording_pairs, run_validation
from adapter.canonical_smoke import GeometryRun
from adapter.config import (
    CURRENT_A,
    MIN_QUIET_RUN_64MS,
    PHASE6_CONTENDERS,
    PROPER_D,
    QUIET_RUN_SCORE,
)
from adapter.contender_policy import apply_candidate_weight_policy, apply_cut_policy
from adapter.diagnostics import geometry_fingerprint, policy_fingerprint
from adapter.tournament_policy import BoundaryEvidence, apply_min_quiet_run, quiet_evidence_score
from referee import BufferScope, Clip, Cutpoint, RecordingReference, SlicerResult, evaluate
from referee.types import (
    EdgeSafetyMetrics,
    EvaluationResult,
    UniqueCutSafetyMetrics,
)


def _cut(*, cut_id: str, time_sec: float, quiet_ms: float, score: float = 1.0) -> SimpleNamespace:
    half = (quiet_ms / 1000.0) / 2.0
    return SimpleNamespace(
        cutpoint_id=cut_id,
        recording_id="r0",
        buffer_id="b0",
        time_sec=time_sec,
        interval_start_sec=time_sec - half,
        interval_end_sec=time_sec + half,
        score=score,
        rms_min_dbfs=-40.0,
        metadata={"run_duration_ms": quiet_ms, "score_max": 0.0},
    )


def _diag(name: str, instance_id: int, workdir: str, ts_hash: str, prob_hash: str) -> GeometryRun:
    from adapter.diagnostics import ExecutionDiagnostics

    config_by_name = {config.name: config for config in PHASE6_CONTENDERS}
    config = config_by_name[name]
    diag = ExecutionDiagnostics(
        geometry_name=name,
        geometry_canonical="{}",
        geometry_fingerprint=geometry_fingerprint(config),
        instance_id=instance_id,
        workdir=workdir,
        vad_cache_dir=workdir + "/vad",
        feature_cache_dir=workdir + "/feat",
        vad_cache_paths=(workdir + "/vad/x.json",),
        feature_cache_paths=(workdir + "/feat/x.json",),
        vad_observation_count=10 + instance_id,
        vad_timestamp_sha256=ts_hash,
        vad_probability_sha256=prob_hash,
        candidate_cutpoint_sha256="cands-" + name,
        selected_clip_sha256="clips-" + name,
        policy_name=name,
        policy_canonical="{}",
        policy_fingerprint=policy_fingerprint(config),
        selected_cutpoint_sha256="cuts-" + name,
    )
    result = SlicerResult(cutpoints=(), clips=())
    return GeometryRun(result=result, diagnostics=diag)


class TestPolicyIdentity:
    def test_a_derived_policies_use_a_geometry_not_d(self) -> None:
        a_fp = geometry_fingerprint(CURRENT_A)
        d_fp = geometry_fingerprint(PROPER_D)
        assert a_fp != d_fp
        assert geometry_fingerprint(MIN_QUIET_RUN_64MS) == a_fp
        assert geometry_fingerprint(QUIET_RUN_SCORE) == a_fp
        assert MIN_QUIET_RUN_64MS.hop_samples == 256
        assert QUIET_RUN_SCORE.hop_samples == 256
        assert MIN_QUIET_RUN_64MS.offsets == (0, 128)
        assert QUIET_RUN_SCORE.offsets == (0, 128)
        assert PROPER_D.hop_samples == 512
        assert PROPER_D.offsets == (0, 128, 256, 384)

    def test_policy_fingerprints_differ_and_are_stable(self) -> None:
        a_pol = policy_fingerprint(CURRENT_A)
        min64 = policy_fingerprint(MIN_QUIET_RUN_64MS)
        quiet = policy_fingerprint(QUIET_RUN_SCORE)
        d_pol = policy_fingerprint(PROPER_D)
        assert a_pol != min64
        assert a_pol != quiet
        assert min64 != quiet
        assert a_pol == d_pol
        assert policy_fingerprint(MIN_QUIET_RUN_64MS) == min64
        assert policy_fingerprint(CURRENT_A) == a_pol

    def test_geometry_fingerprint_ignores_policy_knobs(self) -> None:
        mutated = replace(CURRENT_A, min_quiet_run_ms=64.0, scoring="quiet_evidence")
        assert geometry_fingerprint(mutated) == geometry_fingerprint(CURRENT_A)
        assert policy_fingerprint(mutated) != policy_fingerprint(CURRENT_A)


class TestPolicyLogic:
    def test_min_quiet_gate_accepts_threshold_and_rejects_below(self) -> None:
        ok = apply_min_quiet_run(BoundaryEvidence("ok", 1.0, quiet_run_ms=64.0), 64.0)
        bad = apply_min_quiet_run(BoundaryEvidence("bad", 1.0, quiet_run_ms=63.0), 64.0)
        assert ok.accepted
        assert not bad.accepted
        assert bad.reasons == ("quiet_run_lt_64ms",)

    def test_min_quiet_policy_filters_short_runs_and_baseline_does_not(self) -> None:
        cuts = [
            _cut(cut_id="short", time_sec=2.0, quiet_ms=40.0),
            _cut(cut_id="long", time_sec=8.0, quiet_ms=80.0),
        ]
        kept_a = apply_cut_policy(cuts, CURRENT_A)
        kept_min = apply_cut_policy(cuts, MIN_QUIET_RUN_64MS)
        assert [cut.cutpoint_id for cut in kept_a] == ["short", "long"]
        assert [cut.cutpoint_id for cut in kept_min] == ["long"]

    def test_quiet_run_score_changes_cut_scores_and_candidate_weights(self) -> None:
        cuts = [
            _cut(cut_id="c1", time_sec=2.0, quiet_ms=80.0, score=1.0),
            _cut(cut_id="c2", time_sec=8.0, quiet_ms=200.0, score=1.0),
        ]
        baseline = apply_cut_policy(cuts, CURRENT_A)
        scored = apply_cut_policy(cuts, QUIET_RUN_SCORE)
        assert [cut.score for cut in baseline] == [1.0, 1.0]
        assert scored[0].score != 1.0
        assert scored[1].score > scored[0].score
        expected = quiet_evidence_score(
            BoundaryEvidence(
                "c2",
                8.0,
                quiet_run_ms=200.0,
                quiet_before_ms=100.0,
                quiet_after_ms=100.0,
                base_score=1.0,
                rms_dbfs=-40.0,
            )
        ).score
        assert scored[1].score == pytest.approx(round(expected, 6), abs=1e-3)
        candidates = [
            SimpleNamespace(start_cutpoint_id="c1", end_cutpoint_id="c2", weight=10.0),
        ]
        weighted = apply_candidate_weight_policy(candidates, scored, QUIET_RUN_SCORE)
        unchanged = apply_candidate_weight_policy(candidates, scored, CURRENT_A)
        assert unchanged[0].weight == 10.0
        assert weighted[0].weight != 10.0


class TestFourWayAggregation:
    def test_four_way_table_and_deltas_use_named_contenders(self) -> None:
        def metrics(*, coverage: float, emitted: float, inside: float, gt50: float, gt100: float):
            n = 100
            unique = UniqueCutSafetyMetrics(
                unique_cutpoint_count=n,
                inside_phone_count=int(inside * n),
                inside_phone_rate=inside,
                depth_gt_20ms_count=0,
                depth_gt_50ms_count=int(gt50 * n),
                depth_gt_100ms_count=int(gt100 * n),
                depth_gt_20ms_rate=0.0,
                depth_gt_50ms_rate=gt50,
                depth_gt_100ms_rate=gt100,
            )
            edge = EdgeSafetyMetrics(
                edge_occurrence_count=n,
                inside_phone_count=0,
                inside_phone_rate=0.0,
                depth_gt_20ms_count=0,
                depth_gt_50ms_count=0,
                depth_gt_100ms_count=0,
                depth_gt_20ms_rate=0.0,
                depth_gt_50ms_rate=0.0,
                depth_gt_100ms_rate=0.0,
            )
            score = EvaluationResult(
                unique_cut_safety=unique,
                final_edge_safety=edge,
                eligible_target_speech_sec=100.0,
                retained_target_speech_sec=coverage * 100.0,
                speech_coverage=coverage,
                emitted_audio_sec=emitted,
                clip_count=10,
            )
            return metrics_from_evaluation(score)

        pooled = {
            "current_A": metrics(coverage=0.70, emitted=100.0, inside=0.12, gt50=0.03, gt100=0.005),
            "proper_D": metrics(coverage=0.62, emitted=90.0, inside=0.09, gt50=0.02, gt100=0.004),
            "min_quiet_run_64ms": metrics(coverage=0.60, emitted=85.0, inside=0.11, gt50=0.029, gt100=0.004),
            "quiet_run_score": metrics(coverage=0.68, emitted=98.0, inside=0.11, gt50=0.031, gt100=0.007),
        }
        rows = {row["metric"]: row for row in four_way_comparison_table(pooled)}
        assert rows["speech coverage"]["A"] == pytest.approx(0.70)
        assert rows["speech coverage"]["min_quiet_64"] == pytest.approx(0.60)
        assert rows[">50 ms %"]["quiet_run_score"] == pytest.approx(3.1)
        deltas = {row["metric"]: row for row in delta_vs_a_table(pooled)}
        assert deltas["speech coverage"]["min_quiet_run_64ms"] == pytest.approx(-0.10)
        min64 = interpret_pareto(
            name="min_quiet_run_64ms",
            contender=pooled["min_quiet_run_64ms"],
            pooled_a=pooled["current_A"],
            pooled_d=pooled["proper_D"],
        )
        assert min64["dominated_by_D"] is True
        assert min64["dominated_by_A"] is False

    def test_paired_undefined_gt50_is_counted_not_invented(self) -> None:
        rows = [
            {
                "speaker_id": "s01",
                "recording_id": "r1",
                "geometry": "current_A",
                "speech_coverage": 0.5,
                "depth_gt_50ms_rate": 0.02,
            },
            {
                "speaker_id": "s01",
                "recording_id": "r1",
                "geometry": "min_quiet_run_64ms",
                "speech_coverage": 0.4,
                "depth_gt_50ms_rate": "",
            },
        ]
        paired = paired_recording_comparison(rows, left="current_A", right="min_quiet_run_64ms")
        assert paired["coverage_less"] == 1
        assert paired["gt50ms_undefined"] == 1
        assert paired["contender_safer_gt50ms"] == 0


class TestNoSilentSkipFourWay:
    def test_four_contender_error_aborts_without_summary(self, tmp_path: Path) -> None:
        seen: list[str] = []

        def load(speaker_id: str, recording_id: str) -> LoadedRecording:
            seen.append(recording_id)
            if recording_id == "s0201b":
                raise RuntimeError("boom from second recording")
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

        def run(loaded: LoadedRecording, config) -> GeometryRun:
            ts = "ts-" + config.name
            return _diag(
                config.name,
                instance_id=hash((loaded.recording_id, config.name)) % 10000,
                workdir=f"/tmp/{loaded.recording_id}_{config.name}",
                ts_hash=ts,
                prob_hash="pr-" + config.name,
            )

        def eval_fn(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
            return evaluate(loaded.reference, result)

        with pytest.raises(RuntimeError, match="boom from second recording"):
            run_validation(
                output_dir=tmp_path / "out",
                subset=(("s01", "s0101b"), ("s02", "s0201b")),
                load_fn=load,
                run_fn=run,
                eval_fn=eval_fn,
                contenders=PHASE6_CONTENDERS,
            )
        assert seen == ["s0101b", "s0201b"]
        assert not (tmp_path / "out" / "summary.json").exists()

    def test_four_contender_stub_run_writes_all_rows(self, tmp_path: Path) -> None:
        def load(speaker_id: str, recording_id: str) -> LoadedRecording:
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

        def run(loaded: LoadedRecording, config) -> GeometryRun:
            rec = loaded.recording_id
            if config.name == "current_A":
                result = SlicerResult(
                    cutpoints=(Cutpoint(rec, "buf0", 4.0),),
                    clips=(Clip(rec, "buf0", 2.0, 8.0),),
                )
            elif config.name == "proper_D":
                result = SlicerResult(cutpoints=(), clips=())
            elif config.name == "min_quiet_run_64ms":
                result = SlicerResult(
                    cutpoints=(Cutpoint(rec, "buf0", 5.0),),
                    clips=(Clip(rec, "buf0", 2.0, 7.0),),
                )
            else:
                result = SlicerResult(
                    cutpoints=(Cutpoint(rec, "buf0", 6.0),),
                    clips=(Clip(rec, "buf0", 3.0, 9.0),),
                )
            ts = "ts-" + config.name
            executed = _diag(
                config.name,
                instance_id=hash((loaded.recording_id, config.name)) % 10000,
                workdir=f"/tmp/{loaded.recording_id}_{config.name}",
                ts_hash=ts,
                prob_hash="pr-" + config.name,
            )
            return GeometryRun(result=result, diagnostics=executed.diagnostics)

        def eval_fn(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
            return evaluate(loaded.reference, result)

        summary = run_validation(
            output_dir=tmp_path / "out",
            subset=(("s01", "s0101b"),),
            load_fn=load,
            run_fn=run,
            eval_fn=eval_fn,
            contenders=PHASE6_CONTENDERS,
        )
        assert summary["contender_names"] == [config.name for config in PHASE6_CONTENDERS]
        assert summary["recording_count"] == 1
        assert set(summary["pooled"]) == {config.name for config in PHASE6_CONTENDERS}
        assert summary["four_way"]
        assert summary["delta_vs_A"]
        assert set(summary["paired_vs_A"]) == {
            "proper_D",
            "min_quiet_run_64ms",
            "quiet_run_score",
        }
        assert summary["schedule_differed_from_A_recordings"]["min_quiet_run_64ms"] == 1
        assert summary["schedule_differed_from_A_recordings"]["quiet_run_score"] == 1
        assert (tmp_path / "out" / "summary.json").is_file()
        assert (tmp_path / "out" / "determinism_checks.json").is_file()


def test_canonical_executor_wires_policy_hooks() -> None:
    import inspect

    from adapter import canonical_executor

    source = inspect.getsource(canonical_executor)
    assert "apply_cut_policy(cutpoints, config)" in source
    assert "apply_candidate_weight_policy(candidates, cutpoints, config)" in source


def test_incomplete_four_contender_set_is_not_reusable() -> None:
    rows = [
        {
            "speaker_id": "s01",
            "recording_id": "s0101b",
            "geometry": "current_A",
            "geometry_fingerprint": "fpA",
        },
        {
            "speaker_id": "s01",
            "recording_id": "s0101b",
            "geometry": "proper_D",
            "geometry_fingerprint": "fpD",
        },
    ]
    with pytest.raises(RuntimeError, match="incomplete contender set"):
        completed_recording_pairs(
            rows,
            expected_fingerprints={
                "current_A": "fpA",
                "proper_D": "fpD",
                "min_quiet_run_64ms": "fpA",
                "quiet_run_score": "fpA",
            },
        )


def test_phase5_cohort_fingerprint_constant_matches_derived_cohort() -> None:
    assert len(PHASE5_COHORT_FINGERPRINT) == 64
    int(PHASE5_COHORT_FINGERPRINT, 16)
    assert derive_full_cohort().fingerprint == PHASE5_COHORT_FINGERPRINT
