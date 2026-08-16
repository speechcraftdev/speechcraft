"""Phase 8 round 2: frozen geometry, bundle reuse, fine-scale waveform veto/penalty."""

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
    O25_4_CURRENT,
    O25_4_SHORT_SHALLOW_PENALTY,
    O25_4_WAVEFORM_SILENCE_RATIO,
    O25_4_WEAK_VALLEY_VETO,
    PHASE8_RMS_VARIANTS,
    PHASE8_SMOKE_CONTENDERS,
    PROPER_D,
    RMS_CURRENT,
    RmsPolicySpec,
    rms_policy_payload,
)
from adapter.diagnostics import geometry_fingerprint, policy_canonical_payload, policy_fingerprint
from adapter.feature_bundle import FeatureBundle, require_bundle_geometry
from adapter.rms_evidence import (
    FineRmsGrid,
    build_fine_rms_grid,
    evidence_depth_db,
    half_depth_width_ms,
    percentile,
    recording_silence_threshold,
    recording_speech_ref,
    short_shallow_penalty,
    silence_ratio,
    valley_width_below_threshold_ms,
)
from adapter.rms_policy import apply_rms_policy
from referee import SlicerResult
from referee.types import BufferScope


TRUSTED_A = "72ea8093df3fb11222b7291a784e2272246d6349ad6d5da63900e79412997498"
TRUSTED_O25_4 = "5c718a3fdc193cf92e8babe55085d9a6c49c09e1bb11c69c0d3e71192acf39c2"
TRUSTED_O0_4 = "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"
TRUSTED_O0_2 = "b8f9c3af61c5c18d8b52a1cdb992e5c98e154bc752c5665b52bea987b6d3c947"


def _bundle(
    *,
    fingerprint: str,
    audio: tuple[float, ...] | None = None,
    probs: tuple[float, ...] = (0.1, 0.1, 0.1),
    buffers: tuple[BufferScope, ...] | None = None,
) -> FeatureBundle:
    n = len(probs)
    times = tuple(i * 0.004 for i in range(n))
    return FeatureBundle(
        recording_id="rec0",
        geometry_fingerprint=fingerprint,
        geometry_canonical="{}",
        vad_backend="silero_official_onnx",
        sample_rate_hz=16000,
        audio=audio if audio is not None else (0.2,) * 16000,
        vad_centers_sec=times,
        vad_window_start_sec=times,
        vad_window_end_sec=tuple(t + 0.032 for t in times),
        vad_offset_samples=(0,) * n,
        vad_speech_prob=probs,
        frame_rms_dbfs=(-20.0,) * n,
        frame_voicing=(0.5,) * n,
        buffers=buffers if buffers is not None else (BufferScope("buf0", 0.0, 1.0),),
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
        interval_start_sec=time_sec - 0.1,
        interval_end_sec=time_sec + 0.1,
        score=score,
        metadata={},
    )


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


def _audio_with_dip(
    *,
    center_sec: float,
    width_ms: float,
    speech_amp: float,
    dip_amp: float,
    duration_sec: float = 1.0,
    sample_rate_hz: int = 16000,
) -> tuple[float, ...]:
    n = int(duration_sec * sample_rate_hz)
    audio = [speech_amp] * n
    half = width_ms / 2000.0
    lo = max(0, int((center_sec - half) * sample_rate_hz))
    hi = min(n, int((center_sec + half) * sample_rate_hz))
    for i in range(lo, hi):
        audio[i] = dip_amp
    return tuple(audio)


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

    def test_fingerprints_omit_unused_knobs(self) -> None:
        assert rms_policy_payload(RMS_CURRENT) == {"kind": "current"}
        current = policy_canonical_payload(O25_4_CURRENT)["rms_policy"]
        assert current == {"kind": "current"}
        veto = policy_canonical_payload(O25_4_WEAK_VALLEY_VETO)["rms_policy"]
        assert "prominence_db" not in veto
        assert "score_scale" not in veto
        assert "silence_percentile" not in veto
        assert veto["veto_depth_db"] == 1.5
        assert veto["fine_hop_ms"] == 8.0
        assert veto["speech_percentile"] == 75.0
        assert veto["outer_contrast_db"] == 1.5
        penalty = policy_canonical_payload(O25_4_SHORT_SHALLOW_PENALTY)["rms_policy"]
        assert "veto_depth_db" not in penalty
        assert "silence_percentile" not in penalty
        assert "valley_margin_db" not in penalty
        assert penalty["speech_percentile"] == 75.0
        assert penalty["outer_contrast_db"] == 1.5
        ratio = policy_canonical_payload(O25_4_WAVEFORM_SILENCE_RATIO)["rms_policy"]
        assert "veto_depth_db" not in ratio
        assert "short_valley_ms" not in ratio
        assert "speech_percentile" not in ratio
        assert "outer_contrast_db" not in ratio
        assert ratio["silence_percentile"] == 10.0
        assert ratio["silence_margin_db"] == 0.0

    def test_spec_has_no_dead_prominence_db(self) -> None:
        assert "prominence_db" not in {item.name for item in fields(RmsPolicySpec)}

    def test_smoke_set_is_controls_plus_three_waveform_policies(self) -> None:
        assert tuple(config.name for config in PHASE8_SMOKE_CONTENDERS) == (
            "A_O50_8",
            "O25_4_CURRENT",
            "O25_4_WEAK_VALLEY_VETO",
            "O25_4_SHORT_SHALLOW_PENALTY",
            "O25_4_WAVEFORM_SILENCE_RATIO",
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
            apply_rms_policy([], bundle, O25_4_WEAK_VALLEY_VETO)

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
        broken = replace(O25_4_WEAK_VALLEY_VETO, rms_policy=None)
        with pytest.raises(RuntimeError, match="no rms_policy"):
            apply_rms_policy([], bundle, broken)

    def test_wrong_kind_is_not_current(self) -> None:
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        broken = replace(O25_4_WEAK_VALLEY_VETO, rms_policy=RMS_CURRENT)
        with pytest.raises(RuntimeError, match="kind="):
            apply_rms_policy([], bundle, broken)


class TestFineScaleWaveformEvidence:
    def test_silence_threshold_is_quiet_percentile_not_speech_reference(self) -> None:
        spec = RmsPolicySpec(kind="waveform_silence_ratio")
        rms = (-12.0,) * 75 + (-40.0,) * 25
        threshold = recording_silence_threshold(rms, spec)
        speech_ref = percentile(rms, 75.0)
        assert threshold < speech_ref - 3.0
        assert threshold == pytest.approx(percentile(rms, 10.0))
        assert spec.silence_margin_db == 0.0

    def test_short_and_shallow_is_penalized_short_deep_is_not(self) -> None:
        spec = RmsPolicySpec(kind="short_shallow_penalty")
        short_shallow = short_shallow_penalty(40.0, 2.0, spec)
        short_deep = short_shallow_penalty(40.0, 12.0, spec)
        long_shallow = short_shallow_penalty(120.0, 2.0, spec)
        assert short_shallow > 0.0
        assert short_deep == 0.0
        assert long_shallow == 0.0
        assert short_shallow > short_deep
        assert short_shallow > long_shallow

    def test_local_depth_sees_fine_hops_not_vad_frames(self) -> None:
        spec = RmsPolicySpec(kind="weak_valley_veto")
        times = tuple(i * 0.008 for i in range(80))
        deep = tuple(-12.0 if t < 0.28 or t > 0.36 else -40.0 for t in times)
        shallow = tuple(-12.0 if t < 0.28 or t > 0.36 else -13.0 for t in times)
        speech_ref = recording_speech_ref(deep, spec)
        assert (
            evidence_depth_db(
                times, deep, 0.32, spec, speech_ref, bound_start=0.0, bound_end=1.0
            )
            > spec.veto_depth_db
        )
        assert (
            evidence_depth_db(
                times, shallow, 0.32, spec, speech_ref, bound_start=0.0, bound_end=1.0
            )
            < spec.veto_depth_db
        )

    def test_half_depth_width_tracks_depression_not_whole_recording(self) -> None:
        spec = RmsPolicySpec(kind="short_shallow_penalty")
        times = tuple(i * 0.008 for i in range(80))
        short = tuple(-12.0 if t < 0.30 or t > 0.34 else -20.0 for t in times)
        long = tuple(-12.0 if t < 0.16 or t > 0.48 else -40.0 for t in times)
        short_ref = recording_speech_ref(short, spec)
        long_ref = recording_speech_ref(long, spec)
        assert (
            half_depth_width_ms(
                times, short, 0.32, spec, short_ref, bound_start=0.0, bound_end=1.0
            )
            < spec.short_valley_ms
        )
        assert (
            half_depth_width_ms(
                times, long, 0.32, spec, long_ref, bound_start=0.0, bound_end=1.0
            )
            > spec.short_valley_ms
        )

    def test_valley_width_uses_supplied_threshold(self) -> None:
        times = tuple(i * 0.008 for i in range(40))
        rms = tuple(-12.0 if t < 0.12 or t > 0.20 else -40.0 for t in times)
        wide = valley_width_below_threshold_ms(
            times, rms, 0.16, -20.0, bound_start=0.0, bound_end=1.0
        )
        none = valley_width_below_threshold_ms(
            times, rms, 0.04, -20.0, bound_start=0.0, bound_end=1.0
        )
        assert wide > 50.0
        assert none == 0.0

    def test_silence_ratio_is_high_in_a_real_pause(self) -> None:
        spec = RmsPolicySpec(kind="waveform_silence_ratio")
        times = tuple(i * 0.008 for i in range(80))
        rms = tuple(-12.0 if t < 0.24 or t > 0.40 else -45.0 for t in times)
        threshold = -30.0
        pause = silence_ratio(
            times, rms, 0.32, spec, bound_start=0.0, bound_end=1.0, threshold_db=threshold
        )
        speech = silence_ratio(
            times, rms, 0.08, spec, bound_start=0.0, bound_end=1.0, threshold_db=threshold
        )
        assert pause > 0.5
        assert speech < 0.2
        assert pause > speech


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

    def test_weak_veto_rejects_shallow_keeps_deep_from_waveform(self) -> None:
        shallow_audio = _audio_with_dip(
            center_sec=0.5, width_ms=80.0, speech_amp=0.2, dip_amp=0.18
        )
        deep_audio = _audio_with_dip(
            center_sec=0.5, width_ms=80.0, speech_amp=0.2, dip_amp=0.01
        )
        vad_rms = (-12.0,) * 50
        shallow_bundle = _bundle(
            fingerprint=TRUSTED_O25_4, audio=shallow_audio, probs=(0.1,) * 50
        )
        deep_bundle = _bundle(
            fingerprint=TRUSTED_O25_4, audio=deep_audio, probs=(0.1,) * 50
        )
        object.__setattr__(shallow_bundle, "frame_rms_dbfs", vad_rms)
        object.__setattr__(deep_bundle, "frame_rms_dbfs", vad_rms)
        cut = Cut("c1", "rec0", "buf0", 0.5, 0.4, 0.6, 1.0, {})
        assert apply_rms_policy([cut], shallow_bundle, O25_4_WEAK_VALLEY_VETO) == []
        kept = apply_rms_policy([cut], deep_bundle, O25_4_WEAK_VALLEY_VETO)
        assert len(kept) == 1
        assert kept[0].time_sec == 0.5

    def test_weak_veto_keeps_interior_of_a_long_pause(self) -> None:
        audio = _audio_with_dip(
            center_sec=0.5, width_ms=400.0, speech_amp=0.2, dip_amp=0.01
        )
        bundle = _bundle(fingerprint=TRUSTED_O25_4, audio=audio, probs=(0.1,) * 50)
        cut = Cut("c1", "rec0", "buf0", 0.5, 0.4, 0.6, 1.0, {})
        kept = apply_rms_policy([cut], bundle, O25_4_WEAK_VALLEY_VETO)
        assert len(kept) == 1

    def test_veto_ignores_smeared_vad_frame_rms(self) -> None:
        """VAD-frame RMS looks deep; the waveform is flat speech. Veto the cut."""
        flat = (0.2,) * 16000
        bundle = _bundle(fingerprint=TRUSTED_O25_4, audio=flat, probs=(0.1,) * 50)
        object.__setattr__(
            bundle,
            "frame_rms_dbfs",
            tuple(-12.0 if i < 20 or i > 30 else -40.0 for i in range(50)),
        )
        cut = Cut("c1", "rec0", "buf0", 0.5, 0.4, 0.6, 1.0, {})
        assert apply_rms_policy([cut], bundle, O25_4_WEAK_VALLEY_VETO) == []

    def test_silence_ratio_rescoring_is_order_stable(self) -> None:
        audio = _audio_with_dip(
            center_sec=0.5, width_ms=100.0, speech_amp=0.2, dip_amp=0.01
        )
        bundle = _bundle(fingerprint=TRUSTED_O25_4, audio=audio, probs=(0.1,) * 50)
        cuts = [
            Cut("c1", "rec0", "buf0", 0.16, 0.08, 0.24, 1.0, {}),
            Cut("c2", "rec0", "buf0", 0.50, 0.40, 0.60, 1.0, {}),
        ]
        forward = apply_rms_policy(list(cuts), bundle, O25_4_WAVEFORM_SILENCE_RATIO)
        reverse = apply_rms_policy(
            list(reversed(cuts)), bundle, O25_4_WAVEFORM_SILENCE_RATIO
        )
        by_id_f = {cut.cutpoint_id: cut.score for cut in forward}
        by_id_r = {cut.cutpoint_id: cut.score for cut in reverse}
        assert by_id_f == by_id_r
        assert by_id_f["c2"] > by_id_f["c1"]

    def test_short_shallow_penalty_lowers_score(self) -> None:
        audio = _audio_with_dip(
            center_sec=0.5, width_ms=40.0, speech_amp=0.2, dip_amp=0.16
        )
        bundle = _bundle(fingerprint=TRUSTED_O25_4, audio=audio, probs=(0.1,) * 50)
        cut = Cut("c1", "rec0", "buf0", 0.5, 0.4, 0.6, 1.0, {})
        out = apply_rms_policy([cut], bundle, O25_4_SHORT_SHALLOW_PENALTY)
        assert len(out) == 1
        assert out[0].score < cut.score
        assert out[0].metadata["score_delta"] < 0.0


def _paint(audio: list[float], start_sec: float, end_sec: float, amp: float, sr: int = 16000) -> None:
    lo = max(0, int(round(start_sec * sr)))
    hi = min(len(audio), int(round(end_sec * sr)))
    for i in range(lo, hi):
        audio[i] = amp


def _three_second(*, inside_amp: float, left_amp: float, right_amp: float) -> tuple[float, ...]:
    audio = [inside_amp] * (3 * 16000)
    _paint(audio, 0.0, 1.0, left_amp)
    _paint(audio, 1.0, 2.0, inside_amp)
    _paint(audio, 2.0, 3.0, right_amp)
    return tuple(audio)


class TestBufferScopeIsolation:
    def _scoped(self, audio: tuple[float, ...]) -> FeatureBundle:
        return _bundle(
            fingerprint=TRUSTED_O25_4,
            audio=audio,
            probs=(0.1,) * 50,
            buffers=(BufferScope("buf0", 1.0, 2.0),),
        )

    def _evidence(self, audio: tuple[float, ...], t_sec: float) -> tuple[float | None, float, object, object]:
        spec = O25_4_WEAK_VALLEY_VETO.rms_policy
        assert spec is not None
        bundle = self._scoped(audio)
        grid = build_fine_rms_grid(bundle, spec)
        speech_ref = recording_speech_ref(grid.rms_dbfs, spec)
        depth = evidence_depth_db(
            grid.times_sec,
            grid.rms_dbfs,
            t_sec,
            spec,
            speech_ref,
            bound_start=1.0,
            bound_end=2.0,
        )
        width = half_depth_width_ms(
            grid.times_sec,
            grid.rms_dbfs,
            t_sec,
            spec,
            speech_ref,
            bound_start=1.0,
            bound_end=2.0,
        )
        cut = Cut("c1", "rec0", "buf0", t_sec, t_sec - 0.05, t_sec + 0.05, 1.0, {})
        veto = apply_rms_policy([cut], bundle, O25_4_WEAK_VALLEY_VETO, fine_grid=grid)
        penalty = apply_rms_policy([cut], bundle, O25_4_SHORT_SHALLOW_PENALTY, fine_grid=grid)
        return depth, width, veto, penalty

    def test_left_boundary_out_of_scope_audio_does_not_change_evidence(self) -> None:
        t_sec = 1.02
        baseline = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        loud = _three_second(inside_amp=0.2, left_amp=0.9, right_amp=0.2)
        quiet = _three_second(inside_amp=0.2, left_amp=0.001, right_amp=0.2)
        base = self._evidence(baseline, t_sec)
        assert self._evidence(loud, t_sec) == base
        assert self._evidence(quiet, t_sec) == base

    def test_right_boundary_out_of_scope_audio_does_not_change_evidence(self) -> None:
        t_sec = 1.98
        baseline = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        loud = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.9)
        quiet = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.001)
        base = self._evidence(baseline, t_sec)
        assert self._evidence(loud, t_sec) == base
        assert self._evidence(quiet, t_sec) == base

    def test_fine_grid_and_percentiles_ignore_out_of_scope_audio(self) -> None:
        spec = O25_4_WAVEFORM_SILENCE_RATIO.rms_policy
        assert spec is not None
        clean = self._scoped(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        dirty = self._scoped(_three_second(inside_amp=0.2, left_amp=0.9, right_amp=0.001))
        clean_grid = build_fine_rms_grid(clean, spec)
        dirty_grid = build_fine_rms_grid(dirty, spec)
        assert clean_grid.times_sec
        assert all(1.0 <= t < 2.0 for t in clean_grid.times_sec)
        assert all(1.0 <= t < 2.0 for t in dirty_grid.times_sec)
        assert clean_grid.rms_dbfs == dirty_grid.rms_dbfs
        assert recording_speech_ref(clean_grid.rms_dbfs, spec) == recording_speech_ref(
            dirty_grid.rms_dbfs, spec
        )
        assert recording_silence_threshold(
            clean_grid.rms_dbfs, spec
        ) == recording_silence_threshold(dirty_grid.rms_dbfs, spec)

    def test_valley_width_does_not_walk_out_of_buffer(self) -> None:
        audio = list(_three_second(inside_amp=0.2, left_amp=0.01, right_amp=0.2))
        _paint(audio, 1.0, 1.06, 0.01)
        spec = O25_4_SHORT_SHALLOW_PENALTY.rms_policy
        assert spec is not None
        bundle = self._scoped(tuple(audio))
        grid = build_fine_rms_grid(bundle, spec)
        width = valley_width_below_threshold_ms(
            grid.times_sec,
            grid.rms_dbfs,
            1.02,
            -20.0,
            bound_start=1.0,
            bound_end=2.0,
        )
        assert 0.0 < width < 120.0

    def test_shared_grid_rejects_hop_mismatch(self) -> None:
        spec = O25_4_WEAK_VALLEY_VETO.rms_policy
        assert spec is not None
        bundle = _bundle(fingerprint=TRUSTED_O25_4)
        wrong = FineRmsGrid(times_sec=(0.1,), rms_dbfs=(-20.0,), window_ms=10.0, hop_ms=32.0)
        with pytest.raises(RuntimeError, match="fine RMS grid mismatch"):
            apply_rms_policy([], bundle, O25_4_WEAK_VALLEY_VETO, fine_grid=wrong)

