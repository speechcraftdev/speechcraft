"""Phase 8 RMS/evidence tournament: frozen geometry, bundle reuse, synthetic helpers."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from types import SimpleNamespace

import pytest

from adapter.config import (
    A_O50_8,
    CURRENT_A,
    O0_2,
    O0_4,
    O25_4,
    O25_4_BILATERAL_CONTRAST,
    O25_4_CURRENT,
    O25_4_MULTISCALE_MIN,
    O25_4_MULTISCALE_RMS,
    O25_4_RMS_MIN_PLACEMENT,
    O25_4_RMS_PROMINENCE,
    O25_4_VALLEY_WIDTH_DEPTH,
    PHASE8_RMS_VARIANTS,
    PHASE8_SMOKE_CONTENDERS,
    PROPER_D,
    RMS_CURRENT,
)
from adapter.diagnostics import geometry_fingerprint, policy_fingerprint
from adapter.feature_bundle import FeatureBundle, require_bundle_geometry
from adapter.rms_evidence import (
    argmin_in_bounds,
    bilateral_score,
    clip_interval,
    multiscale_pause_score,
    placement_grid,
    prominence_drops_db,
    prominence_score,
    valley_shape_score,
    valley_width_ms,
)
from adapter.rms_policy import apply_rms_policy
from referee import SlicerResult
from referee.types import BufferScope


TRUSTED_A = "72ea8093df3fb11222b7291a784e2272246d6349ad6d5da63900e79412997498"
TRUSTED_O25_4 = "5c718a3fdc193cf92e8babe55085d9a6c49c09e1bb11c69c0d3e71192acf39c2"
TRUSTED_O0_4 = "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"
TRUSTED_O0_2 = "b8f9c3af61c5c18d8b52a1cdb992e5c98e154bc752c5665b52bea987b6d3c947"


def _spec():
    return RMS_CURRENT


def _bundle(*, fingerprint: str, probs: tuple[float, ...] = (0.1, 0.1, 0.1)) -> FeatureBundle:
    n = len(probs)
    times = tuple(i * 0.004 for i in range(n))
    return FeatureBundle(
        recording_id="rec0",
        geometry_fingerprint=fingerprint,
        geometry_canonical="{}",
        vad_backend="silero_official_onnx",
        sample_rate_hz=16000,
        audio=(0.0,) * 16000,
        vad_centers_sec=times,
        vad_window_start_sec=times,
        vad_window_end_sec=tuple(t + 0.032 for t in times),
        vad_offset_samples=(0,) * n,
        vad_speech_prob=probs,
        frame_rms_dbfs=(-20.0,) * n,
        frame_voicing=(0.5,) * n,
        buffers=(BufferScope("buf0", 0.0, 1.0),),
        vad_observation_count=n,
        vad_timestamp_sha256="ts",
        vad_probability_sha256="pr",
        bundle_id="bundle-1",
    )


def _cut(*, time_sec: float = 0.5, score: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        cutpoint_id="c1",
        recording_id="rec0",
        buffer_id="buf0",
        time_sec=time_sec,
        interval_start_sec=0.4,
        interval_end_sec=0.6,
        score=score,
        metadata={},
    )


class TestFrozenGeometries:
    def test_a_o25_o0_fingerprints_unchanged(self) -> None:
        assert geometry_fingerprint(A_O50_8) == TRUSTED_A
        assert geometry_fingerprint(CURRENT_A) == TRUSTED_A
        assert geometry_fingerprint(O25_4) == TRUSTED_O25_4
        assert geometry_fingerprint(O0_4) == TRUSTED_O0_4
        assert geometry_fingerprint(O0_2) == TRUSTED_O0_2
        assert geometry_fingerprint(PROPER_D) != TRUSTED_A

    def test_rms_variants_use_o25_4_geometry_only(self) -> None:
        for config in PHASE8_RMS_VARIANTS:
            assert config.window_samples == O25_4.window_samples
            assert config.hop_samples == O25_4.hop_samples
            assert config.offsets == O25_4.offsets
            assert geometry_fingerprint(config) == TRUSTED_O25_4
            assert config.rms_policy is not None

    def test_controls_have_no_rms_policy(self) -> None:
        for config in (A_O50_8, O25_4, O0_4, O0_2):
            assert config.rms_policy is None
            assert config.scoring is None
            assert config.min_quiet_run_ms is None

    def test_policy_fingerprints_deterministic_and_distinct(self) -> None:
        fingerprints = [policy_fingerprint(config) for config in PHASE8_RMS_VARIANTS]
        assert len(set(fingerprints)) == len(fingerprints)
        assert policy_fingerprint(O25_4_CURRENT) == policy_fingerprint(O25_4_CURRENT)
        assert policy_fingerprint(O25_4_CURRENT) != policy_fingerprint(O25_4)
        assert policy_fingerprint(O25_4_RMS_PROMINENCE) != policy_fingerprint(O25_4_CURRENT)

    def test_smoke_set_is_the_specified_ten(self) -> None:
        assert tuple(config.name for config in PHASE8_SMOKE_CONTENDERS) == (
            "A_O50_8",
            "O25_4_CURRENT",
            "O25_4_RMS_PROMINENCE",
            "O25_4_MULTISCALE_RMS",
            "O25_4_VALLEY_WIDTH_DEPTH",
            "O25_4_BILATERAL_CONTRAST",
            "O25_4_RMS_MIN_PLACEMENT",
            "O25_4_MULTISCALE_MIN",
            "O0_4",
            "O0_2",
        )


class TestFeatureBundleReuse:
    def test_bundle_rejects_cross_geometry(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        require_bundle_geometry(bundle, O25_4_CURRENT)
        with pytest.raises(RuntimeError, match="geometry mismatch"):
            require_bundle_geometry(bundle, A_O50_8)
        with pytest.raises(RuntimeError, match="geometry mismatch"):
            require_bundle_geometry(bundle, O0_4)
        with pytest.raises(RuntimeError, match="geometry mismatch"):
            require_bundle_geometry(bundle, O0_2)

    def test_policy_rejects_foreign_bundle(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_A)
        with pytest.raises(RuntimeError, match="geometry mismatch"):
            apply_rms_policy([], bundle, O25_4_RMS_PROMINENCE)

    def test_bundle_is_read_only(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        with pytest.raises((TypeError, ValueError, AttributeError)):
            bundle.vad_speech_prob[0] = 0.99  # type: ignore[index]
        with pytest.raises((TypeError, ValueError, AttributeError)):
            bundle.audio[0] = 1.0  # type: ignore[index]
        names = {item.name for item in fields(bundle)}
        assert "phones" not in names
        assert "uncertainty_intervals" not in names
        assert "reference" not in names

    def test_missing_rms_policy_does_not_silently_become_current(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        broken = replace(O25_4_RMS_PROMINENCE, rms_policy=None)
        with pytest.raises(RuntimeError, match="no rms_policy"):
            apply_rms_policy([], bundle, broken)

    def test_wrong_kind_is_not_current(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        broken = replace(O25_4_RMS_PROMINENCE, rms_policy=RMS_CURRENT)
        with pytest.raises(RuntimeError, match="kind="):
            apply_rms_policy([], bundle, broken)


class TestSyntheticRmsEvidence:
    def test_clean_pause_outranks_consonant_dip(self) -> None:
        spec = _spec()
        times = tuple(i * 0.004 for i in range(80))
        pause = []
        dip = []
        for t in times:
            if 0.14 <= t <= 0.18:
                pause.append(-45.0)
            else:
                pause.append(-12.0)
            if 0.156 <= t <= 0.164:
                dip.append(-22.0)
            else:
                dip.append(-12.0)
        pause_left, pause_right = prominence_drops_db(times, pause, 0.16, spec)
        dip_left, dip_right = prominence_drops_db(times, dip, 0.16, spec)
        assert prominence_score(pause_left, pause_right) > prominence_score(dip_left, dip_right)
        assert multiscale_pause_score(times, pause, 0.16, spec) > multiscale_pause_score(
            times, dip, 0.16, spec
        )

    def test_bilateral_penalizes_one_sided_low_region(self) -> None:
        spec = _spec()
        times = tuple(i * 0.004 for i in range(80))
        one_sided = [-12.0 if t < 0.16 else -40.0 for t in times]
        two_sided = [-12.0 if t < 0.14 or t > 0.18 else -40.0 for t in times]
        left_os, right_os = prominence_drops_db(times, one_sided, 0.16, spec)
        left_ok, right_ok = prominence_drops_db(times, two_sided, 0.16, spec)
        assert bilateral_score(left_os, right_os) < bilateral_score(left_ok, right_ok)

    def test_wider_valley_scores_higher_without_hard_gate(self) -> None:
        spec = _spec()
        times = tuple(i * 0.004 for i in range(80))
        wide = [-12.0 if t < 0.12 or t > 0.20 else -40.0 for t in times]
        narrow = [-12.0 if t < 0.15 or t > 0.17 else -40.0 for t in times]
        assert valley_width_ms(times, wide, 0.16, spec) > valley_width_ms(times, narrow, 0.16, spec)
        assert valley_shape_score(times, wide, 0.16, spec) > valley_shape_score(
            times, narrow, 0.16, spec
        )
        assert valley_width_ms(times, narrow, 0.16, spec) > 0.0

    def test_min_placement_picks_true_minimum(self) -> None:
        times = (0.10, 0.12, 0.14, 0.16, 0.18)
        rms = (-20.0, -22.0, -30.0, -21.0, -19.0)
        assert argmin_in_bounds(times, rms, 0.10, 0.18) == pytest.approx(0.14)

    def test_placement_never_crosses_buffer(self) -> None:
        audio = tuple(0.2 if i < 8000 else 0.01 for i in range(16000))
        lo, hi = clip_interval(0.4, 0.8, 0.0, 0.5)
        assert hi == pytest.approx(0.5)
        times, values = placement_grid(
            audio=audio,
            sample_rate_hz=16000,
            start_sec=lo,
            end_sec=hi,
            window_ms=24.0,
            hop_ms=4.0,
        )
        assert times
        assert max(times) <= 0.5 + 1e-9
        placed = argmin_in_bounds(times, values, lo, hi)
        assert placed is not None
        assert lo <= placed <= hi


class TestSelectedScheduleAndEvaluator:
    def test_slicer_result_still_only_cutpoints_and_clips(self) -> None:
        assert {item.name for item in fields(SlicerResult)} == {"cutpoints", "clips"}

    def test_current_policy_is_identity_on_candidates(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        cut = _cut()
        out = apply_rms_policy([cut], bundle, O25_4_CURRENT)
        assert len(out) == 1
        assert out[0].time_sec == cut.time_sec
        assert out[0].score == cut.score

    def test_prominence_rescoring_is_order_stable(self) -> None:
        @dataclass(frozen=True)
        class Cut:
            cutpoint_id: str
            recording_id: str
            buffer_id: str
            time_sec: float
            interval_start_sec: float
            interval_end_sec: float
            score: float
            metadata: dict

        bundle = _bundle(fingerprint=TRUSTED_O25_4, probs=(0.1,) * 50)
        object.__setattr__(bundle, "frame_rms_dbfs", tuple(-12.0 if i < 20 or i > 30 else -40.0 for i in range(50)))
        object.__setattr__(bundle, "vad_centers_sec", tuple(i * 0.004 for i in range(50)))
        cuts = [
            Cut("c1", "rec0", "buf0", 0.10, 0.08, 0.12, 1.0, {}),
            Cut("c2", "rec0", "buf0", 0.16, 0.14, 0.18, 1.0, {}),
        ]
        forward = apply_rms_policy(list(cuts), bundle, O25_4_RMS_PROMINENCE)
        reverse = apply_rms_policy(list(reversed(cuts)), bundle, O25_4_RMS_PROMINENCE)
        by_id_f = {cut.cutpoint_id: cut.score for cut in forward}
        by_id_r = {cut.cutpoint_id: cut.score for cut in reverse}
        assert by_id_f == by_id_r
        assert by_id_f["c2"] != 1.0
