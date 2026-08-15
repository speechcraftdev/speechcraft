"""Focused Phase-5 full-run logic tests. Phase 1–4 tests stay elsewhere."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from adapter.buckeye_loader import (
    VALIDATION_SUBSET,
    LoadedRecording,
    cohort_identity_fingerprint,
    default_spot_check_ids,
    derive_full_cohort,
)
from adapter.buckeye_metrics import (
    paired_recording_comparison,
    pool_metric_dicts,
)
from adapter.buckeye_validate import (
    completed_recording_pairs,
    run_validation,
)
from adapter.canonical_smoke import GeometryRun
from adapter.config import CURRENT_A, PROPER_D, GeometryConfig
from adapter.diagnostics import ExecutionDiagnostics, geometry_fingerprint
from referee import BufferScope, Clip, Cutpoint, RecordingReference, SlicerResult, evaluate
from referee.types import EvaluationResult


def _diag(name: str, instance_id: int, workdir: str, ts_hash: str, prob_hash: str) -> ExecutionDiagnostics:
    return ExecutionDiagnostics(
        geometry_name=name,
        geometry_canonical="{}",
        geometry_fingerprint="fp-" + name,
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


def _independent_run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
    name = config.name
    ts = "ts-A" if config is CURRENT_A else "ts-D"
    prob = "pr-A" if config is CURRENT_A else "pr-D"
    if config is CURRENT_A:
        result = SlicerResult(
            cutpoints=(Cutpoint(loaded.recording_id, "buf0", 4.0),),
            clips=(Clip(loaded.recording_id, "buf0", 2.0, 8.0),),
        )
    else:
        result = SlicerResult(cutpoints=(), clips=())
    diag = _diag(
        name,
        instance_id=hash((loaded.recording_id, name, id(config))) % 10000,
        workdir=f"/tmp/{loaded.recording_id}_{name}_{id(config)}",
        ts_hash=ts,
        prob_hash=prob,
    )
    diag = replace(diag, geometry_fingerprint=geometry_fingerprint(config))
    return GeometryRun(result=result, diagnostics=diag)


class TestCohortDerivation:
    def test_identity_fingerprint_is_order_sensitive_and_stable(self) -> None:
        first = (("s01", "s0101b"), ("s02", "s0201b"))
        swapped = (("s02", "s0201b"), ("s01", "s0101b"))
        assert cohort_identity_fingerprint(first) == cohort_identity_fingerprint(first)
        assert cohort_identity_fingerprint(first) != cohort_identity_fingerprint(swapped)
        assert len(cohort_identity_fingerprint(first)) == 64

    def test_spot_check_ids_are_first_middle_last_and_s0301a(self) -> None:
        recordings = tuple((f"s{i:02d}", f"s{i:02d}01a") for i in range(1, 6))
        recordings = recordings + (("s03", "s0301a"),)
        chosen = default_spot_check_ids(recordings)
        assert chosen[0] == recordings[0]
        assert chosen[1] == recordings[len(recordings) // 2]
        assert chosen[2] == recordings[-1]
        assert ("s03", "s0301a") in chosen

    def test_full_cohort_is_deterministic_from_canonical_artifacts(self) -> None:
        first = derive_full_cohort()
        second = derive_full_cohort()
        assert first.recordings == second.recordings
        assert first.fingerprint == second.fingerprint
        assert first.speaker_count == 23
        assert first.recording_count == 120
        assert first.allowed_buffer_duration_sec == pytest.approx(39984.791, abs=0.01)
        assert first.recordings[0] == ("s01", "s0101b")
        assert first.recordings[-1] == ("s24", "s2403b")
        assert ("s03", "s0301a") in first.recordings
        assert VALIDATION_SUBSET[0] in first.recordings
        assert first.fingerprint == cohort_identity_fingerprint(first.recordings)


class TestPooledAndPairedMath:
    def test_pool_metric_dicts_uses_counts_not_mean_percentages(self) -> None:
        rows = [
            {
                "unique_cutpoint_count": 10,
                "inside_phone_count": 5,
                "inside_phone_rate": 0.5,
                "depth_gt_20ms_count": 2,
                "depth_gt_50ms_count": 1,
                "depth_gt_100ms_count": 0,
                "depth_gt_20ms_rate": 0.2,
                "depth_gt_50ms_rate": 0.1,
                "depth_gt_100ms_rate": 0.0,
                "edge_occurrence_count": 4,
                "edge_inside_phone_count": 2,
                "edge_inside_phone_rate": 0.5,
                "edge_depth_gt_20ms_count": 1,
                "edge_depth_gt_50ms_count": 1,
                "edge_depth_gt_100ms_count": 0,
                "edge_depth_gt_20ms_rate": 0.25,
                "edge_depth_gt_50ms_rate": 0.25,
                "edge_depth_gt_100ms_rate": 0.0,
                "eligible_target_speech_sec": 10.0,
                "retained_target_speech_sec": 8.0,
                "speech_coverage": 0.8,
                "emitted_audio_sec": 12.0,
                "clip_count": 2,
            },
            {
                "unique_cutpoint_count": 90,
                "inside_phone_count": 0,
                "inside_phone_rate": 0.0,
                "depth_gt_20ms_count": 0,
                "depth_gt_50ms_count": 0,
                "depth_gt_100ms_count": 0,
                "depth_gt_20ms_rate": 0.0,
                "depth_gt_50ms_rate": 0.0,
                "depth_gt_100ms_rate": 0.0,
                "edge_occurrence_count": 6,
                "edge_inside_phone_count": 0,
                "edge_inside_phone_rate": 0.0,
                "edge_depth_gt_20ms_count": 0,
                "edge_depth_gt_50ms_count": 0,
                "edge_depth_gt_100ms_count": 0,
                "edge_depth_gt_20ms_rate": 0.0,
                "edge_depth_gt_50ms_rate": 0.0,
                "edge_depth_gt_100ms_rate": 0.0,
                "eligible_target_speech_sec": 90.0,
                "retained_target_speech_sec": 45.0,
                "speech_coverage": 0.5,
                "emitted_audio_sec": 40.0,
                "clip_count": 8,
            },
        ]
        pooled = pool_metric_dicts(rows)
        assert pooled.inside_phone_rate == pytest.approx(5 / 100)
        assert pooled.speech_coverage == pytest.approx(53.0 / 100.0)
        assert pooled.speech_coverage != pytest.approx((0.8 + 0.5) / 2.0)
        assert pooled.mean_clip_duration_sec == pytest.approx(52.0 / 10.0)

    def test_paired_coverage_counts_wins_losses_ties_and_median(self) -> None:
        rows = [
            _pair_row("s01", "r1", "current_A", 0.70, 0.04),
            _pair_row("s01", "r1", "proper_D", 0.60, 0.02),
            _pair_row("s02", "r2", "current_A", 0.50, 0.03),
            _pair_row("s02", "r2", "proper_D", 0.55, 0.03),
            _pair_row("s03", "r3", "current_A", 0.40, 0.01),
            _pair_row("s03", "r3", "proper_D", 0.40, 0.02),
        ]
        paired = paired_recording_comparison(rows)
        assert paired["d_coverage_greater"] == 1
        assert paired["d_coverage_less"] == 1
        assert paired["coverage_ties"] == 1
        assert paired["mean_d_minus_a_coverage"] == pytest.approx((-0.10 + 0.05 + 0.0) / 3.0)
        assert paired["median_d_minus_a_coverage"] == pytest.approx(0.0)
        assert paired["d_safer_gt50ms"] == 1
        assert paired["a_safer_gt50ms"] == 1
        assert paired["gt50ms_ties"] == 1


def _pair_row(
    speaker_id: str,
    recording_id: str,
    geometry: str,
    coverage: float,
    gt50: float,
) -> dict[str, object]:
    return {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "geometry": geometry,
        "speech_coverage": coverage,
        "depth_gt_50ms_rate": gt50,
    }


class TestNoSilentSkip:
    def test_independence_failure_identifies_recording_and_writes_no_summary(
        self, tmp_path: Path
    ) -> None:
        seen: list[str] = []

        def load(speaker_id: str, recording_id: str) -> LoadedRecording:
            seen.append(recording_id)
            return _loaded(tmp_path, speaker_id, recording_id)

        def run(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
            base = _independent_run(loaded, config)
            return GeometryRun(
                result=base.result,
                diagnostics=replace(base.diagnostics, geometry_fingerprint="collapsed"),
            )

        def eval_fn(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
            return evaluate(loaded.reference, result)

        with pytest.raises(RuntimeError, match="A/D independence failed on s01/s0101b"):
            run_validation(
                output_dir=tmp_path / "out",
                subset=VALIDATION_SUBSET,
                load_fn=load,
                run_fn=run,
                eval_fn=eval_fn,
            )
        assert seen == ["s0101b"]
        assert "s0201b" not in seen
        assert not (tmp_path / "out" / "summary.json").exists()


class TestResumeValidation:
    def test_incomplete_pair_is_not_reusable(self) -> None:
        rows = [
            {
                "speaker_id": "s01",
                "recording_id": "s0101b",
                "geometry": "current_A",
                "geometry_fingerprint": "fpA",
            }
        ]
        with pytest.raises(RuntimeError, match="incomplete A/D pair"):
            completed_recording_pairs(
                rows,
                expected_a_fingerprint="fpA",
                expected_d_fingerprint="fpD",
            )

    def test_fingerprint_mismatch_rejects_resume_rows(self) -> None:
        rows = [
            {
                "speaker_id": "s01",
                "recording_id": "s0101b",
                "geometry": "current_A",
                "geometry_fingerprint": "wrong",
            },
            {
                "speaker_id": "s01",
                "recording_id": "s0101b",
                "geometry": "proper_D",
                "geometry_fingerprint": "fpD",
            },
        ]
        with pytest.raises(RuntimeError, match="resume geometry fingerprint mismatch"):
            completed_recording_pairs(
                rows,
                expected_a_fingerprint="fpA",
                expected_d_fingerprint="fpD",
            )

    def test_resume_reuses_matching_completed_rows(self, tmp_path: Path) -> None:
        seen_loads: list[str] = []

        def load(speaker_id: str, recording_id: str) -> LoadedRecording:
            seen_loads.append(recording_id)
            return _loaded(tmp_path, speaker_id, recording_id)

        def eval_fn(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
            return evaluate(loaded.reference, result)

        out = tmp_path / "out"
        run_validation(
            output_dir=out,
            subset=VALIDATION_SUBSET[:1],
            load_fn=load,
            run_fn=_independent_run,
            eval_fn=eval_fn,
            resume=False,
        )
        first_loads = list(seen_loads)
        assert first_loads.count("s0101b") >= 1

        def load_resume(speaker_id: str, recording_id: str) -> LoadedRecording:
            seen_loads.append("resume-" + recording_id)
            return _loaded(tmp_path, speaker_id, recording_id)

        run_validation(
            output_dir=out,
            subset=VALIDATION_SUBSET[:1],
            load_fn=load_resume,
            run_fn=_independent_run,
            eval_fn=eval_fn,
            resume=True,
        )
        main_loop_reloads = [
            item for item in seen_loads[len(first_loads) :] if not item.startswith("resume-")
        ]
        assert main_loop_reloads == []
        assert "resume-s0101b" in seen_loads
        a_fp = geometry_fingerprint(CURRENT_A)
        d_fp = geometry_fingerprint(PROPER_D)
        rows = completed_recording_pairs(
            [
                {
                    "speaker_id": "s01",
                    "recording_id": "s0101b",
                    "geometry": "current_A",
                    "geometry_fingerprint": a_fp,
                },
                {
                    "speaker_id": "s01",
                    "recording_id": "s0101b",
                    "geometry": "proper_D",
                    "geometry_fingerprint": d_fp,
                },
            ],
            expected_a_fingerprint=a_fp,
            expected_d_fingerprint=d_fp,
        )
        assert ("s01", "s0101b") in rows
