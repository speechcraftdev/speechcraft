"""Focused Phase-4 orchestration tests. No fake full Buckeye corpus replica."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from adapter.buckeye_loader import (
    VALIDATION_SUBSET,
    BuckeyePaths,
    LoadedRecording,
    load_recording,
    validation_subset,
)
from adapter.buckeye_metrics import (
    comparison_table,
    metrics_from_evaluation,
    pool_evaluations,
)
from adapter.buckeye_validate import run_validation
from adapter.canonical_smoke import GeometryRun
from adapter.config import CURRENT_A, GeometryConfig
from adapter.diagnostics import ExecutionDiagnostics, geometry_fingerprint
from adapter.types import SlicerRequest
from referee import (
    BufferScope,
    Clip,
    Cutpoint,
    PhoneInterval,
    RecordingReference,
    SlicerResult,
    evaluate,
)
from referee.types import (
    EdgeSafetyMetrics,
    EvaluationResult,
    UniqueCutSafetyMetrics,
)


def _unique(
    *,
    n: int,
    inside: int,
    gt20: int = 0,
    gt50: int = 0,
    gt100: int = 0,
) -> UniqueCutSafetyMetrics:
    return UniqueCutSafetyMetrics(
        unique_cutpoint_count=n,
        inside_phone_count=inside,
        inside_phone_rate=None if n == 0 else inside / n,
        depth_gt_20ms_count=gt20,
        depth_gt_50ms_count=gt50,
        depth_gt_100ms_count=gt100,
        depth_gt_20ms_rate=None if n == 0 else gt20 / n,
        depth_gt_50ms_rate=None if n == 0 else gt50 / n,
        depth_gt_100ms_rate=None if n == 0 else gt100 / n,
    )


def _edge(
    *,
    n: int,
    inside: int,
    gt20: int = 0,
    gt50: int = 0,
    gt100: int = 0,
) -> EdgeSafetyMetrics:
    return EdgeSafetyMetrics(
        edge_occurrence_count=n,
        inside_phone_count=inside,
        inside_phone_rate=None if n == 0 else inside / n,
        depth_gt_20ms_count=gt20,
        depth_gt_50ms_count=gt50,
        depth_gt_100ms_count=gt100,
        depth_gt_20ms_rate=None if n == 0 else gt20 / n,
        depth_gt_50ms_rate=None if n == 0 else gt50 / n,
        depth_gt_100ms_rate=None if n == 0 else gt100 / n,
    )


def _score(
    *,
    unique: UniqueCutSafetyMetrics,
    edge: EdgeSafetyMetrics,
    eligible: float,
    retained: float,
    emitted: float,
    clips: int,
) -> EvaluationResult:
    coverage = None if eligible == 0.0 else retained / eligible
    return EvaluationResult(
        unique_cut_safety=unique,
        final_edge_safety=edge,
        eligible_target_speech_sec=eligible,
        retained_target_speech_sec=retained,
        speech_coverage=coverage,
        emitted_audio_sec=emitted,
        clip_count=clips,
    )


def _diag(
    name: str,
    instance_id: int,
    workdir: str,
    ts_hash: str,
    prob_hash: str,
    fingerprint: str,
) -> ExecutionDiagnostics:
    return ExecutionDiagnostics(
        geometry_name=name,
        geometry_canonical="{}",
        geometry_fingerprint=fingerprint,
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
    )


class TestFrozenSubset:
    def test_subset_is_the_fixed_recording_list(self) -> None:
        assert validation_subset() == VALIDATION_SUBSET
        assert VALIDATION_SUBSET == (
            ("s01", "s0101b"),
            ("s02", "s0201b"),
            ("s03", "s0301a"),
            ("s04", "s0401b"),
        )
        speakers = [row[0] for row in VALIDATION_SUBSET]
        assert speakers == ["s01", "s02", "s03", "s04"]


class TestAggregation:
    def test_pool_sums_counts_and_recomputes_rates_from_totals(self) -> None:
        first = _score(
            unique=_unique(n=10, inside=2, gt20=2, gt50=1, gt100=0),
            edge=_edge(n=6, inside=1, gt20=1, gt50=0, gt100=0),
            eligible=10.0,
            retained=4.0,
            emitted=20.0,
            clips=3,
        )
        second = _score(
            unique=_unique(n=6, inside=0, gt20=0, gt50=0, gt100=0),
            edge=_edge(n=4, inside=0, gt20=0, gt50=0, gt100=0),
            eligible=5.0,
            retained=5.0,
            emitted=8.0,
            clips=2,
        )
        pooled = pool_evaluations([first, second])
        assert pooled.unique_cutpoint_count == 16
        assert pooled.inside_phone_count == 2
        assert pooled.inside_phone_rate == pytest.approx(2 / 16)
        assert pooled.depth_gt_50ms_count == 1
        assert pooled.depth_gt_50ms_rate == pytest.approx(1 / 16)
        assert pooled.eligible_target_speech_sec == pytest.approx(15.0)
        assert pooled.retained_target_speech_sec == pytest.approx(9.0)
        assert pooled.speech_coverage == pytest.approx(9.0 / 15.0)
        assert pooled.emitted_audio_sec == pytest.approx(28.0)
        assert pooled.clip_count == 5
        assert first.speech_coverage == pytest.approx(0.4)
        assert second.speech_coverage == pytest.approx(1.0)
        assert pooled.speech_coverage != pytest.approx(
            (first.speech_coverage + second.speech_coverage) / 2.0
        )

    def test_per_recording_metrics_copy_evaluator_fields(self) -> None:
        reference = RecordingReference(
            recording_id="rec0",
            buffers=(BufferScope("buf0", 0.0, 30.0),),
            phones=(PhoneInterval("rec0", "buf0", 5.0, 6.0, "AA"),),
            uncertainty_intervals=(),
        )
        result = SlicerResult(
            cutpoints=(Cutpoint("rec0", "buf0", 5.4),),
            clips=(Clip("rec0", "buf0", 2.0, 6.0),),
        )
        score = evaluate(reference, result)
        copied = metrics_from_evaluation(score)
        assert copied.unique_cutpoint_count == score.unique_cut_safety.unique_cutpoint_count
        assert copied.inside_phone_count == score.unique_cut_safety.inside_phone_count
        assert copied.inside_phone_rate == score.unique_cut_safety.inside_phone_rate
        assert copied.depth_gt_20ms_count == score.unique_cut_safety.depth_gt_20ms_count
        assert copied.speech_coverage == score.speech_coverage
        assert copied.emitted_audio_sec == score.emitted_audio_sec
        assert copied.clip_count == score.clip_count
        assert copied.edge_inside_phone_count == score.final_edge_safety.inside_phone_count

    def test_comparison_table_uses_pooled_evaluator_metrics(self) -> None:
        pooled_a = metrics_from_evaluation(
            _score(
                unique=_unique(n=10, inside=2, gt20=2, gt50=1, gt100=0),
                edge=_edge(n=8, inside=2, gt20=1, gt50=1, gt100=0),
                eligible=100.0,
                retained=68.0,
                emitted=80.0,
                clips=12,
            )
        )
        pooled_d = metrics_from_evaluation(
            _score(
                unique=_unique(n=8, inside=1, gt20=1, gt50=0, gt100=0),
                edge=_edge(n=6, inside=1, gt20=1, gt50=0, gt100=0),
                eligible=100.0,
                retained=62.0,
                emitted=70.0,
                clips=10,
            )
        )
        rows = {row["metric"]: row for row in comparison_table(pooled_a, pooled_d)}
        assert rows["speech coverage"]["A"] == pooled_a.speech_coverage
        assert rows["speech coverage"]["D"] == pooled_d.speech_coverage
        assert rows["emitted sec"]["A"] == pooled_a.emitted_audio_sec
        assert rows["clips"]["A"] == pooled_a.clip_count
        assert rows["selected cuts"]["A"] == pooled_a.unique_cutpoint_count
        assert rows["inside-phone %"]["A"] == pytest.approx(pooled_a.inside_phone_rate * 100.0)
        assert rows[">50 ms %"]["D"] == pytest.approx(pooled_d.depth_gt_50ms_rate * 100.0)
        assert rows["final-edge inside-phone %"]["A"] == pytest.approx(
            pooled_a.edge_inside_phone_rate * 100.0
        )


class TestLoaderFailures:
    def test_missing_speaker_dir_fails_loudly(self, tmp_path: Path) -> None:
        paths = BuckeyePaths(
            normalized_root=tmp_path / "corpus",
            cohort_root=tmp_path / "cohort",
            cluster_mapping_csv=tmp_path / "cohort" / "benchmark_cluster_mapping.csv",
        )
        with pytest.raises(FileNotFoundError, match="normalized speaker directory missing"):
            load_recording("s99", "s9901a", paths=paths)

    def test_missing_accepted_recording_fails_loudly(self) -> None:
        with pytest.raises(RuntimeError, match="not in the frozen accepted reconciliation"):
            load_recording("s01", "s0199z")


class TestRunDoesNotSkip:
    def test_one_recording_error_aborts_the_run(self, tmp_path: Path) -> None:
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

        def run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
            result = SlicerResult(cutpoints=(), clips=())
            name = config.name
            ts = "ts-A" if config is CURRENT_A else "ts-D"
            prob = "pr-A" if config is CURRENT_A else "pr-D"
            diag = _diag(
                name,
                instance_id=hash((loaded.recording_id, name)) % 10000,
                workdir=f"/tmp/{loaded.recording_id}_{name}",
                ts_hash=ts,
                prob_hash=prob,
                fingerprint=geometry_fingerprint(config),
            )
            return GeometryRun(result=result, diagnostics=diag)

        def eval_fn(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
            return evaluate(loaded.reference, result)

        with pytest.raises(RuntimeError, match="boom from second recording"):
            run_validation(
                output_dir=tmp_path / "out",
                subset=VALIDATION_SUBSET,
                load_fn=load,
                run_fn=run,
                eval_fn=eval_fn,
            )
        assert seen == ["s0101b", "s0201b"]
        assert "s0301a" not in seen
        assert not (tmp_path / "out" / "summary.json").exists()


class TestSlicerRequestIsolation:
    def test_slicer_request_cannot_carry_reference_annotations(self) -> None:
        names = {f.name for f in fields(SlicerRequest)}
        assert "phones" not in names
        assert "uncertainty_intervals" not in names
        assert "reference" not in names
        assert names == {"recording_id", "audio_path", "sample_rate_hz", "buffers", "config"}


class TestRealCanonicalLoad:
    def test_load_first_subset_recording(self) -> None:
        loaded = load_recording("s01", "s0101b")
        assert loaded.recording_id == "s0101b"
        assert loaded.audio_path.is_file()
        assert loaded.sample_rate_hz == 16000
        assert loaded.buffers
        assert loaded.reference.phones
        assert loaded.reference.recording_id == "s0101b"
        request = SlicerRequest(
            recording_id=loaded.recording_id,
            audio_path=loaded.audio_path,
            sample_rate_hz=loaded.sample_rate_hz,
            buffers=loaded.buffers,
            config=CURRENT_A,
        )
        assert not hasattr(request, "phones")
        evaluate(loaded.reference, SlicerResult(cutpoints=(), clips=()))
