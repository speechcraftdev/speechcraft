"""Phase 10A: 15-feature P(bad) logistic scoring on frozen O0_4. Synthetic/unit only."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from adapter.config import (
    ALL,
    FEATURE_NAMES,
    FEATURE_SUBSET_ALL,
    FEATURE_SUBSET_RMS_WAVEFORM,
    FEATURE_SUBSET_VAD_ONLY,
    LOGISTIC_BOUNDARY_V1_UNTRAINED,
    O0_2,
    O0_4,
    O0_4_LOGISTIC,
    O25_4,
    PHASE7_GEOMETRIES,
    PHASE8_SMOKE_CONTENDERS,
    PHASE10_LOGISTIC_CONTENDERS,
    RMS_WAVEFORM,
    VAD_ONLY,
    LogisticSpec,
    feature_names_for_subset,
    logistic_policy_payload,
    resolve_geometries,
    untrained_logistic_spec,
)
from adapter.contender_policy import apply_candidate_weight_policy, apply_cut_policy
from adapter.diagnostics import geometry_fingerprint, policy_canonical_payload, policy_fingerprint
from adapter.feature_bundle import FeatureBundle, _FORBIDDEN_BUNDLE_FIELDS
from adapter.logistic_features import LOGISTIC_FINE_RMS_SPEC, extract_all_boundary_features
from adapter.logistic_model import predict_p_bad, preference_delta, sigmoid, standardize
from adapter.logistic_policy import apply_logistic_cut_policy, require_logistic_spec
from adapter.rms_evidence import build_fine_rms_grid
from adapter.rms_policy import family_fine_rms_spec
from referee.types import BufferScope


TRUSTED_O0_4 = "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"
SR = 16000
IDX = {name: index for index, name in enumerate(FEATURE_NAMES)}


def _cut(*, cut_id: str, time_sec: float, score: float = 1.0, buffer_id: str = "buf0") -> SimpleNamespace:
    return SimpleNamespace(
        cutpoint_id=cut_id,
        recording_id="rec0",
        buffer_id=buffer_id,
        time_sec=time_sec,
        interval_start_sec=time_sec - 0.04,
        interval_end_sec=time_sec + 0.04,
        score=score,
        rms_min_dbfs=-40.0,
        metadata={},
    )


def _paint(audio: list[float], start_sec: float, end_sec: float, amp: float) -> None:
    lo = max(0, int(round(start_sec * SR)))
    hi = min(len(audio), int(round(end_sec * SR)))
    for i in range(lo, hi):
        audio[i] = amp


def _three_second(*, inside_amp: float, left_amp: float, right_amp: float) -> tuple[float, ...]:
    audio = [inside_amp] * (3 * SR)
    _paint(audio, 0.0, 1.0, left_amp)
    _paint(audio, 1.0, 2.0, inside_amp)
    _paint(audio, 2.0, 3.0, right_amp)
    return tuple(audio)


def _sine(freq_hz: float, *, amp: float = 0.2, duration_sec: float = 3.0) -> tuple[float, ...]:
    n = int(duration_sec * SR)
    return tuple(amp * math.sin(2.0 * math.pi * freq_hz * i / SR) for i in range(n))


def _det_noise(*, amp: float = 0.2, duration_sec: float = 3.0, seed: int = 1) -> tuple[float, ...]:
    n = int(duration_sec * SR)
    x = int(seed)
    samples: list[float] = []
    for _ in range(n):
        x = (1_103_515_245 * x + 12_345) % (2**31)
        samples.append(amp * ((x / float(2**30)) - 1.0))
    return tuple(samples)


def _vad_track(*, inside: float, left: float, right: float, hop_sec: float = 0.004) -> tuple[tuple[float, ...], tuple[float, ...]]:
    n = int(round(3.0 / hop_sec))
    times = tuple(i * hop_sec for i in range(n))
    probs = []
    for time_sec in times:
        if time_sec < 1.0:
            probs.append(left)
        elif time_sec < 2.0:
            probs.append(inside)
        else:
            probs.append(right)
    return times, tuple(probs)


def _bundle(
    *,
    audio: tuple[float, ...],
    vad_times: tuple[float, ...],
    vad_probs: tuple[float, ...],
    buffers: tuple[BufferScope, ...] = (BufferScope("buf0", 1.0, 2.0),),
    fingerprint: str = TRUSTED_O0_4,
) -> FeatureBundle:
    n = len(vad_probs)
    return FeatureBundle(
        recording_id="rec0",
        geometry_fingerprint=fingerprint,
        geometry_canonical="{}",
        vad_backend="silero_official_onnx",
        sample_rate_hz=SR,
        audio=audio,
        vad_centers_sec=vad_times,
        vad_window_start_sec=vad_times,
        vad_window_end_sec=tuple(t + 0.032 for t in vad_times),
        vad_offset_samples=(0,) * n,
        vad_speech_prob=vad_probs,
        frame_rms_dbfs=(-20.0,) * n,
        frame_voicing=(0.5,) * n,
        buffers=buffers,
        vad_observation_count=n,
        vad_timestamp_sha256="ts",
        vad_probability_sha256="pr",
        bundle_id="bundle-1",
    )


def _extract(bundle: FeatureBundle, t_sec: float) -> tuple[float, ...]:
    grid = build_fine_rms_grid(bundle, LOGISTIC_FINE_RMS_SPEC)
    bound_start, bound_end = 1.0, 2.0
    return extract_all_boundary_features(
        audio=bundle.audio,
        sample_rate_hz=SR,
        t_sec=t_sec,
        bound_start=bound_start,
        bound_end=bound_end,
        fine_grid=grid,
        vad_centers_sec=bundle.vad_centers_sec,
        vad_speech_prob=bundle.vad_speech_prob,
        vad_threshold=0.2,
    )


def _p_bad_spec(*, center_coef: float) -> LogisticSpec:
    n = len(FEATURE_NAMES)
    return LogisticSpec(
        kind="boundary_v1",
        model_id="unit_p_bad",
        target="p_bad",
        feature_subset=FEATURE_SUBSET_ALL,
        feature_names=FEATURE_NAMES,
        feature_mean=(0.0,) * n,
        feature_scale=(1.0,) * n,
        coefficients=(center_coef,) + (0.0,) * (n - 1),
        intercept=0.0,
        score_scale=1.0,
    )


class TestFrozenO04:
    def test_o0_4_geometry_and_policy_remain_unset(self) -> None:
        assert O0_4.name == "O0_4"
        assert O0_4.window_samples == 512
        assert O0_4.hop_samples == 512
        assert O0_4.offsets == (0, 64, 128, 192, 256, 320, 384, 448)
        assert O0_4.sample_rate_hz == 16000
        assert O0_4.scoring is None
        assert O0_4.rms_policy is None
        assert O0_4.logistic is None
        assert O0_4.min_quiet_run_ms is None
        assert geometry_fingerprint(O0_4) == TRUSTED_O0_4

    def test_logistic_variant_shares_geometry_not_policy(self) -> None:
        assert O0_4_LOGISTIC.window_samples == O0_4.window_samples
        assert O0_4_LOGISTIC.hop_samples == O0_4.hop_samples
        assert O0_4_LOGISTIC.offsets == O0_4.offsets
        assert O0_4_LOGISTIC.sample_rate_hz == O0_4.sample_rate_hz
        assert geometry_fingerprint(O0_4_LOGISTIC) == TRUSTED_O0_4
        assert O0_4_LOGISTIC.scoring == "logistic_boundary"
        assert O0_4_LOGISTIC.logistic == LOGISTIC_BOUNDARY_V1_UNTRAINED
        assert O0_4_LOGISTIC.logistic is not None
        assert O0_4_LOGISTIC.logistic.target == "p_bad"
        assert policy_fingerprint(O0_4_LOGISTIC) != policy_fingerprint(O0_4)
        assert policy_fingerprint(O0_4_LOGISTIC) == policy_fingerprint(O0_4_LOGISTIC)

    def test_logistic_is_not_in_earlier_tournaments(self) -> None:
        names = {config.name for config in PHASE7_GEOMETRIES}
        smoke = {config.name for config in PHASE8_SMOKE_CONTENDERS}
        assert "O0_4_LOGISTIC" not in names
        assert "O0_4_LOGISTIC" not in smoke
        assert tuple(config.name for config in PHASE10_LOGISTIC_CONTENDERS) == (
            "O0_4",
            "O0_4_LOGISTIC",
        )
        resolved = resolve_geometries("O0_4,O0_4_LOGISTIC")
        assert tuple(config.name for config in resolved) == ("O0_4", "O0_4_LOGISTIC")

    def test_geometry_fingerprint_ignores_logistic_spec(self) -> None:
        mutated = replace(O0_4, scoring="logistic_boundary", logistic=LOGISTIC_BOUNDARY_V1_UNTRAINED)
        assert geometry_fingerprint(mutated) == geometry_fingerprint(O0_4)
        assert geometry_fingerprint(O0_4) != geometry_fingerprint(O25_4)
        assert geometry_fingerprint(O0_4) != geometry_fingerprint(O0_2)


class TestFeatureSchema:
    def test_fifteen_named_features_and_subsets(self) -> None:
        assert len(FEATURE_NAMES) == 15
        assert FEATURE_NAMES == (
            "center_rms",
            "rms_percentile",
            "rms_valley_depth",
            "rms_valley_width",
            "left_rms",
            "right_rms",
            "rms_asymmetry",
            "zcr_40ms",
            "zcr_80ms",
            "abs_amplitude_p90",
            "short_window_energy_variance",
            "spectral_flatness",
            "high_frequency_energy_fraction",
            "vad_mean_64ms",
            "low_vad_run_width",
        )
        assert RMS_WAVEFORM == FEATURE_NAMES[:13]
        assert VAD_ONLY == FEATURE_NAMES[13:]
        assert ALL == FEATURE_NAMES
        assert feature_names_for_subset(FEATURE_SUBSET_ALL) == ALL
        assert feature_names_for_subset(FEATURE_SUBSET_RMS_WAVEFORM) == RMS_WAVEFORM
        assert feature_names_for_subset(FEATURE_SUBSET_VAD_ONLY) == VAD_ONLY
        assert set(FEATURE_NAMES).isdisjoint(_FORBIDDEN_BUNDLE_FIELDS)
        assert "quiet_run_norm" not in FEATURE_NAMES

    def test_spec_is_immutable_and_payload_includes_standardization(self) -> None:
        with pytest.raises(FrozenInstanceError):
            LOGISTIC_BOUNDARY_V1_UNTRAINED.intercept = 1.0  # type: ignore[misc]
        payload = logistic_policy_payload(LOGISTIC_BOUNDARY_V1_UNTRAINED)
        assert payload["target"] == "p_bad"
        assert payload["feature_subset"] == "all"
        assert payload["feature_names"] == list(FEATURE_NAMES)
        assert payload["coefficients"] == [0.0] * 15
        assert payload["feature_mean"] == [0.0] * 15
        assert payload["feature_scale"] == [1.0] * 15
        assert payload["intercept"] == 0.0
        assert "weights" not in payload
        assert "bias" not in payload
        assert policy_canonical_payload(O0_4).get("logistic") is None
        assert policy_canonical_payload(O0_4_LOGISTIC)["logistic"] == payload
        with pytest.raises(RuntimeError, match="unknown logistic.kind"):
            logistic_policy_payload(replace(LOGISTIC_BOUNDARY_V1_UNTRAINED, kind="nope"))
        with pytest.raises(RuntimeError, match="p_bad"):
            logistic_policy_payload(replace(LOGISTIC_BOUNDARY_V1_UNTRAINED, target="p_good"))


class TestLogisticInference:
    def test_untrained_zero_model_is_p_bad_half(self) -> None:
        assert sigmoid(0.0) == 0.5
        raw = tuple(float(i) for i in range(15))
        assert predict_p_bad(raw, LOGISTIC_BOUNDARY_V1_UNTRAINED) == 0.5
        assert preference_delta(0.5, LOGISTIC_BOUNDARY_V1_UNTRAINED) == 0.0

    def test_standardize_uses_mean_and_scale(self) -> None:
        spec = replace(
            LOGISTIC_BOUNDARY_V1_UNTRAINED,
            feature_mean=(3.0,) + (0.0,) * 14,
            feature_scale=(2.0,) + (1.0,) * 14,
        )
        raw = (5.0,) + (0.0,) * 14
        assert standardize(raw, spec)[0] == pytest.approx(1.0)
        with pytest.raises(RuntimeError, match="strictly positive"):
            replace(spec, feature_scale=(0.0,) + (1.0,) * 14)
        with pytest.raises(RuntimeError, match="non-finite"):
            replace(spec, intercept=float("nan"))

    def test_higher_p_bad_lowers_preference(self) -> None:
        spec = _p_bad_spec(center_coef=1.0)
        high = (8.0,) + (0.0,) * 14
        low = (-8.0,) + (0.0,) * 14
        p_high = predict_p_bad(high, spec)
        p_low = predict_p_bad(low, spec)
        assert p_high > p_low
        assert preference_delta(p_high, spec) < preference_delta(p_low, spec)
        assert preference_delta(p_high, spec) < 0.0
        assert preference_delta(p_low, spec) > 0.0


class TestWaveformExtraction:
    def test_extracted_vector_has_fifteen_features(self) -> None:
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        bundle = _bundle(audio=_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2), vad_times=vad_times, vad_probs=vad_probs)
        features = _extract(bundle, 1.5)
        assert len(features) == 15
        assert features == _extract(bundle, 1.5)
        assert all(math.isfinite(value) for value in features)

    def test_silence_features_are_finite(self) -> None:
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        bundle = _bundle(audio=(0.0,) * (3 * SR), vad_times=vad_times, vad_probs=vad_probs)
        features = _extract(bundle, 1.5)
        assert len(features) == 15
        assert all(math.isfinite(v) for v in features)

    def test_nan_waveform_is_rejected(self) -> None:
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        audio = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        audio[int(1.5 * SR)] = float("nan")
        bundle = _bundle(audio=tuple(audio), vad_times=vad_times, vad_probs=vad_probs)
        with pytest.raises(RuntimeError, match="non-finite logistic feature"):
            _extract(bundle, 1.5)

    def test_family_grid_is_requested_for_logistic(self) -> None:
        assert family_fine_rms_spec([O0_4]) is None
        spec = family_fine_rms_spec([O0_4, O0_4_LOGISTIC])
        assert spec is not None
        assert spec.fine_window_ms == 10.0
        assert spec.fine_hop_ms == 8.0
        vad_only = replace(O0_4_LOGISTIC, logistic=untrained_logistic_spec(FEATURE_SUBSET_VAD_ONLY))
        assert family_fine_rms_spec([vad_only]) is None


class TestFeatureSemantics:
    def _from_audio(self, audio: tuple[float, ...], t_sec: float = 1.5) -> tuple[float, ...]:
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        return _extract(_bundle(audio=audio, vad_times=vad_times, vad_probs=vad_probs), t_sec)

    def test_low_frequency_sine_has_lower_zcr_than_high_frequency(self) -> None:
        low = self._from_audio(_sine(200.0))
        high = self._from_audio(_sine(2000.0))
        assert low[IDX["zcr_40ms"]] < high[IDX["zcr_40ms"]]
        assert low[IDX["zcr_80ms"]] < high[IDX["zcr_80ms"]]

    def test_high_frequency_sine_has_higher_high_band_energy(self) -> None:
        low = self._from_audio(_sine(400.0))
        high = self._from_audio(_sine(6000.0))
        assert high[IDX["high_frequency_energy_fraction"]] > low[IDX["high_frequency_energy_fraction"]]

    def test_noise_is_flatter_than_a_tone(self) -> None:
        tone = self._from_audio(_sine(1000.0))
        noise = self._from_audio(_det_noise())
        assert noise[IDX["spectral_flatness"]] > tone[IDX["spectral_flatness"]]

    def test_louder_waveform_raises_abs_amplitude_p90(self) -> None:
        quiet = self._from_audio(_sine(1000.0, amp=0.05))
        loud = self._from_audio(_sine(1000.0, amp=0.5))
        assert loud[IDX["abs_amplitude_p90"]] > quiet[IDX["abs_amplitude_p90"]]

    def test_constant_amplitude_has_lower_energy_variance_than_steps(self) -> None:
        constant = self._from_audio(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        stepped = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        t = 1.46
        while t < 1.54:
            _paint(stepped, t, t + 0.01, 0.05 if int(round(t * 100)) % 2 == 0 else 0.45)
            t += 0.01
        modulated = self._from_audio(tuple(stepped))
        assert constant[IDX["short_window_energy_variance"]] < modulated[IDX["short_window_energy_variance"]]

    def test_rms_valley_is_deeper_and_wider_than_flat(self) -> None:
        flat = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        dipped = list(flat)
        _paint(dipped, 1.44, 1.56, 0.01)
        flat_f = self._from_audio(tuple(flat))
        dip_f = self._from_audio(tuple(dipped))
        assert dip_f[IDX["rms_valley_depth"]] > flat_f[IDX["rms_valley_depth"]]
        assert dip_f[IDX["rms_valley_width"]] > flat_f[IDX["rms_valley_width"]]

    def test_rms_asymmetry_sign_is_left_minus_right_dbfs(self) -> None:
        left_loud = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        _paint(left_loud, 1.0, 1.5, 0.5)
        _paint(left_loud, 1.5, 2.0, 0.05)
        right_loud = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        _paint(right_loud, 1.0, 1.5, 0.05)
        _paint(right_loud, 1.5, 2.0, 0.5)
        left_f = self._from_audio(tuple(left_loud))
        right_f = self._from_audio(tuple(right_loud))
        assert left_f[IDX["left_rms"]] > left_f[IDX["right_rms"]]
        assert left_f[IDX["rms_asymmetry"]] > 0.0
        assert right_f[IDX["rms_asymmetry"]] < 0.0
        assert left_f[IDX["rms_asymmetry"]] == pytest.approx(
            left_f[IDX["left_rms"]] - left_f[IDX["right_rms"]]
        )

    def test_low_vad_run_width_grows_with_the_low_run(self) -> None:
        audio = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        hop = 0.004
        n = int(round(3.0 / hop))
        times = tuple(i * hop for i in range(n))

        def width(low_start: float, low_end: float) -> float:
            probs = tuple(0.05 if low_start <= t < low_end else 0.9 for t in times)
            return _extract(_bundle(audio=audio, vad_times=times, vad_probs=probs), 1.50)[IDX["low_vad_run_width"]]

        short = width(1.48, 1.52)
        long = width(1.20, 1.80)
        one_hop = width(1.500, 1.504)
        assert long > short
        assert one_hop > 0.0
        assert one_hop < 20.0


class TestBufferScopeIsolation:
    def _features(self, audio: tuple[float, ...], t_sec: float, *, left_vad: float = 0.1, right_vad: float = 0.1, inside_vad: float = 0.1) -> tuple[float, ...]:
        vad_times, vad_probs = _vad_track(inside=inside_vad, left=left_vad, right=right_vad)
        bundle = _bundle(audio=audio, vad_times=vad_times, vad_probs=vad_probs)
        return _extract(bundle, t_sec)

    def test_mid_buffer_ignores_outside_waveform(self) -> None:
        t_sec = 1.5
        baseline = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        loud = _three_second(inside_amp=0.2, left_amp=0.95, right_amp=0.95)
        quiet = _three_second(inside_amp=0.2, left_amp=0.001, right_amp=0.001)
        base = self._features(baseline, t_sec)
        assert len(base) == 15
        assert self._features(loud, t_sec) == base
        assert self._features(quiet, t_sec) == base

    def test_left_wall_clips_rms_waveform_spectral_and_vad(self) -> None:
        t_sec = 1.02
        baseline = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        loud = _three_second(inside_amp=0.2, left_amp=0.95, right_amp=0.2)
        poisoned_vad = self._features(baseline, t_sec, left_vad=0.99, right_vad=0.1)
        low_vad = self._features(baseline, t_sec, left_vad=0.0, right_vad=0.1)
        base = self._features(baseline, t_sec)
        assert self._features(loud, t_sec) == base
        assert poisoned_vad == base
        assert low_vad == base

    def test_right_wall_clips_rms_waveform_spectral_and_vad(self) -> None:
        t_sec = 1.98
        baseline = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        loud = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.95)
        poisoned_vad = self._features(baseline, t_sec, left_vad=0.1, right_vad=0.99)
        low_vad = self._features(baseline, t_sec, left_vad=0.1, right_vad=0.0)
        base = self._features(baseline, t_sec)
        assert self._features(loud, t_sec) == base
        assert poisoned_vad == base
        assert low_vad == base

    def test_low_vad_run_stops_at_buffer_wall(self) -> None:
        t_sec = 1.12
        audio = _three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2)
        hop = 0.004
        n = int(round(3.0 / hop))
        times = tuple(i * hop for i in range(n))

        def probs(low_start: float, low_end: float) -> tuple[float, ...]:
            return tuple(0.05 if low_start <= t < low_end else 0.9 for t in times)

        def width(vad_probs: tuple[float, ...]) -> float:
            bundle = _bundle(audio=audio, vad_times=times, vad_probs=vad_probs)
            return _extract(bundle, t_sec)[14]

        clipped = width(probs(0.50, 1.30))
        high_left = width(probs(1.00, 1.30))
        assert clipped == high_left
        assert clipped < 400.0
        assert clipped > 100.0


class TestLogisticPolicy:
    def _clean_bundle(self) -> FeatureBundle:
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        return _bundle(
            audio=_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2),
            vad_times=vad_times,
            vad_probs=vad_probs,
        )

    def test_untrained_logistic_does_not_change_scores_or_weights(self) -> None:
        bundle = self._clean_bundle()
        cuts = [
            _cut(cut_id="a", time_sec=1.3, score=1.0),
            _cut(cut_id="b", time_sec=1.7, score=1.4),
        ]
        baseline = apply_cut_policy(cuts, O0_4)
        scored = apply_cut_policy(cuts, O0_4_LOGISTIC, bundle=bundle)
        assert [cut.cutpoint_id for cut in baseline] == ["a", "b"]
        assert [cut.cutpoint_id for cut in scored] == ["a", "b"]
        assert [cut.score for cut in scored] == [cut.score for cut in baseline]
        assert scored[0].metadata["logistic_p_bad"] == 0.5
        assert scored[0].metadata["score_delta"] == 0.0
        assert "logistic_prob" not in scored[0].metadata
        candidates = [SimpleNamespace(start_cutpoint_id="a", end_cutpoint_id="b", weight=10.0)]
        assert apply_candidate_weight_policy(candidates, scored, O0_4_LOGISTIC)[0].weight == 10.0

    def test_higher_p_bad_lowers_candidate_preference(self) -> None:
        audio = list(_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2))
        _paint(audio, 1.28, 1.32, 0.8)
        _paint(audio, 1.68, 1.72, 0.01)
        vad_times, vad_probs = _vad_track(inside=0.1, left=0.1, right=0.1)
        bundle = _bundle(audio=tuple(audio), vad_times=vad_times, vad_probs=vad_probs)
        config = replace(O0_4_LOGISTIC, logistic=_p_bad_spec(center_coef=1.0))
        cuts = [
            _cut(cut_id="loud", time_sec=1.30, score=1.0),
            _cut(cut_id="quiet", time_sec=1.70, score=1.0),
        ]
        scored = apply_cut_policy(cuts, config, bundle=bundle)
        by_id = {cut.cutpoint_id: cut for cut in scored}
        assert by_id["loud"].metadata["logistic_p_bad"] > by_id["quiet"].metadata["logistic_p_bad"]
        assert by_id["loud"].score < by_id["quiet"].score
        mid = _cut(cut_id="mid", time_sec=1.50, score=1.0)
        scored_three = apply_cut_policy([cuts[0], mid, cuts[1]], config, bundle=bundle)
        candidates = [
            SimpleNamespace(start_cutpoint_id="loud", end_cutpoint_id="mid", weight=10.0),
            SimpleNamespace(start_cutpoint_id="quiet", end_cutpoint_id="mid", weight=10.0),
        ]
        weighted = apply_candidate_weight_policy(candidates, scored_three, config)
        assert weighted[0].weight < weighted[1].weight
        unchanged = apply_candidate_weight_policy(candidates, scored_three, O0_4)
        assert unchanged[0].weight == 10.0

    def test_guards_reject_mismatched_specs(self) -> None:
        bundle = self._clean_bundle()
        cuts = [_cut(cut_id="c", time_sec=1.5)]
        missing = replace(O0_4, name="broken", scoring="logistic_boundary")
        with pytest.raises(RuntimeError, match="no logistic spec"):
            apply_cut_policy(cuts, missing, bundle=bundle)
        dangling = replace(O0_4, name="broken", logistic=LOGISTIC_BOUNDARY_V1_UNTRAINED)
        with pytest.raises(RuntimeError, match="logistic spec but scoring"):
            apply_cut_policy(cuts, dangling, bundle=bundle)
        with pytest.raises(RuntimeError, match="unknown scoring policy"):
            apply_cut_policy(cuts, replace(O0_4, name="broken", scoring="not_a_policy"))
        with pytest.raises(RuntimeError, match="requires a feature bundle"):
            apply_cut_policy(cuts, O0_4_LOGISTIC)
        bad_names = LogisticSpec(
            kind="boundary_v1",
            model_id="bad",
            target="p_bad",
            feature_subset="all",
            feature_names=("nope",),
            feature_mean=(0.0,),
            feature_scale=(1.0,),
            coefficients=(0.0,),
        )
        with pytest.raises(RuntimeError, match="feature_names"):
            require_logistic_spec(replace(O0_4_LOGISTIC, logistic=bad_names))
        with pytest.raises(RuntimeError, match="not logistic_boundary"):
            apply_logistic_cut_policy(cuts, O0_4, bundle)
        foreign = _bundle(
            audio=_three_second(inside_amp=0.2, left_amp=0.2, right_amp=0.2),
            vad_times=_vad_track(inside=0.1, left=0.1, right=0.1)[0],
            vad_probs=_vad_track(inside=0.1, left=0.1, right=0.1)[1],
            fingerprint=geometry_fingerprint(O25_4),
        )
        with pytest.raises(RuntimeError, match="geometry mismatch"):
            apply_cut_policy(cuts, O0_4_LOGISTIC, bundle=foreign)

    def test_does_not_drop_candidates(self) -> None:
        bundle = self._clean_bundle()
        cuts = [
            _cut(cut_id="a", time_sec=1.2),
            _cut(cut_id="b", time_sec=1.5),
            _cut(cut_id="c", time_sec=1.8),
        ]
        scored = apply_cut_policy(cuts, O0_4_LOGISTIC, bundle=bundle)
        assert [cut.cutpoint_id for cut in scored] == ["a", "b", "c"]
