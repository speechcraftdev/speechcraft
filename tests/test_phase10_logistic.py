"""Phase 10A: logistic boundary scoring on frozen O0_4. Synthetic/unit only."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from adapter.config import (
    LOGISTIC_BOUNDARY_FEATURE_NAMES,
    LOGISTIC_BOUNDARY_V1_UNTRAINED,
    O0_2,
    O0_4,
    O0_4_LOGISTIC,
    O25_4,
    PHASE7_GEOMETRIES,
    PHASE8_SMOKE_CONTENDERS,
    PHASE10_LOGISTIC_CONTENDERS,
    LogisticSpec,
    logistic_policy_payload,
    resolve_geometries,
)
from adapter.contender_policy import apply_candidate_weight_policy, apply_cut_policy
from adapter.diagnostics import geometry_fingerprint, policy_canonical_payload, policy_fingerprint
from adapter.feature_bundle import _FORBIDDEN_BUNDLE_FIELDS
from adapter.logistic_features import features_from_cut, scale_boundary_features
from adapter.logistic_model import fit_logistic, predict_proba, sigmoid
from adapter.logistic_policy import apply_logistic_cut_policy, require_logistic_spec


TRUSTED_O0_4 = "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"


def _cut(
    *,
    cut_id: str,
    time_sec: float,
    quiet_ms: float,
    score: float = 1.0,
    rms_dbfs: float = -40.0,
    prominence_db: float = 0.0,
) -> SimpleNamespace:
    half = (quiet_ms / 1000.0) / 2.0
    return SimpleNamespace(
        cutpoint_id=cut_id,
        recording_id="r0",
        buffer_id="b0",
        time_sec=time_sec,
        interval_start_sec=time_sec - half,
        interval_end_sec=time_sec + half,
        score=score,
        rms_min_dbfs=rms_dbfs,
        metadata={"run_duration_ms": quiet_ms, "score_max": prominence_db},
    )


def _separable_training() -> tuple[list[tuple[float, ...]], list[float]]:
    features: list[tuple[float, ...]] = []
    labels: list[float] = []
    for quiet in (16.0, 24.0, 32.0, 40.0, 48.0, 180.0, 200.0, 220.0, 240.0, 250.0):
        positive = quiet >= 180.0
        features.append(
            scale_boundary_features(
                quiet_run_ms=quiet,
                quiet_before_ms=quiet / 2.0,
                quiet_after_ms=quiet / 2.0,
                rms_dbfs=-20.0 if not positive else -55.0,
                local_prominence_db=0.0 if not positive else 8.0,
            )
        )
        labels.append(1.0 if positive else 0.0)
    return features, labels


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


class TestLogisticSpec:
    def test_spec_is_immutable_and_payload_is_stable(self) -> None:
        with pytest.raises(FrozenInstanceError):
            LOGISTIC_BOUNDARY_V1_UNTRAINED.bias = 1.0  # type: ignore[misc]
        payload = logistic_policy_payload(LOGISTIC_BOUNDARY_V1_UNTRAINED)
        assert payload == {
            "kind": "boundary_v1",
            "model_id": "untrained_zero",
            "feature_names": list(LOGISTIC_BOUNDARY_FEATURE_NAMES),
            "weights": [0.0] * 5,
            "bias": 0.0,
            "score_scale": 1.0,
        }
        assert policy_canonical_payload(O0_4).get("logistic") is None
        assert policy_canonical_payload(O0_4_LOGISTIC)["logistic"] == payload
        with pytest.raises(RuntimeError, match="unknown logistic.kind"):
            logistic_policy_payload(replace(LOGISTIC_BOUNDARY_V1_UNTRAINED, kind="nope"))

    def test_feature_names_do_not_leak_annotations(self) -> None:
        assert LOGISTIC_BOUNDARY_FEATURE_NAMES == (
            "quiet_run_norm",
            "quiet_before_norm",
            "quiet_after_norm",
            "rms_quiet_norm",
            "prominence_norm",
        )
        assert set(LOGISTIC_BOUNDARY_FEATURE_NAMES).isdisjoint(_FORBIDDEN_BUNDLE_FIELDS)


class TestLogisticMath:
    def test_untrained_zero_model_is_p_half(self) -> None:
        assert sigmoid(0.0) == 0.5
        zeros = (0.0, 0.0, 0.0, 0.0, 0.0)
        assert predict_proba(zeros, LOGISTIC_BOUNDARY_V1_UNTRAINED) == 0.5
        features = scale_boundary_features(
            quiet_run_ms=200.0,
            quiet_before_ms=100.0,
            quiet_after_ms=100.0,
            rms_dbfs=-50.0,
            local_prominence_db=4.0,
        )
        assert predict_proba(features, LOGISTIC_BOUNDARY_V1_UNTRAINED) == 0.5

    def test_features_from_cut_are_deterministic(self) -> None:
        cut = _cut(cut_id="c", time_sec=2.0, quiet_ms=80.0, rms_dbfs=-40.0, prominence_db=2.0)
        first = features_from_cut(cut)
        second = features_from_cut(cut)
        assert first == second
        assert first == pytest.approx(
            scale_boundary_features(
                quiet_run_ms=80.0,
                quiet_before_ms=40.0,
                quiet_after_ms=40.0,
                rms_dbfs=-40.0,
                local_prominence_db=2.0,
            )
        )

    def test_fit_on_synthetic_labels_ranks_quiet_cuts_higher(self) -> None:
        x_rows, y_rows = _separable_training()
        first = fit_logistic(x_rows, y_rows, model_id="synthetic_fit")
        second = fit_logistic(x_rows, y_rows, model_id="synthetic_fit")
        assert first.weights == second.weights
        assert first.bias == second.bias
        assert first.model_id == "synthetic_fit"
        low = scale_boundary_features(
            quiet_run_ms=24.0,
            quiet_before_ms=12.0,
            quiet_after_ms=12.0,
            rms_dbfs=-20.0,
            local_prominence_db=0.0,
        )
        high = scale_boundary_features(
            quiet_run_ms=240.0,
            quiet_before_ms=120.0,
            quiet_after_ms=120.0,
            rms_dbfs=-55.0,
            local_prominence_db=8.0,
        )
        assert predict_proba(high, first) > predict_proba(low, first)
        assert predict_proba(high, first) > 0.5
        assert predict_proba(low, first) < 0.5

    def test_fit_rejects_empty_and_non_binary(self) -> None:
        with pytest.raises(RuntimeError, match="at least one example"):
            fit_logistic([], [])
        row = scale_boundary_features(
            quiet_run_ms=80.0,
            quiet_before_ms=40.0,
            quiet_after_ms=40.0,
            rms_dbfs=-40.0,
            local_prominence_db=0.0,
        )
        with pytest.raises(RuntimeError, match="0 or 1"):
            fit_logistic([row], [0.5])
        with pytest.raises(RuntimeError, match="length mismatch"):
            fit_logistic([row], [1.0, 0.0])
        with pytest.raises(RuntimeError, match="has 2 features"):
            fit_logistic([(0.1, 0.2)], [1.0])


class TestLogisticPolicy:
    def test_untrained_logistic_does_not_change_scores_or_weights(self) -> None:
        cuts = [
            _cut(cut_id="short", time_sec=2.0, quiet_ms=40.0, score=1.0),
            _cut(cut_id="long", time_sec=8.0, quiet_ms=200.0, score=1.4),
        ]
        baseline = apply_cut_policy(cuts, O0_4)
        scored = apply_cut_policy(cuts, O0_4_LOGISTIC)
        assert [cut.cutpoint_id for cut in baseline] == ["short", "long"]
        assert [cut.cutpoint_id for cut in scored] == ["short", "long"]
        assert [cut.score for cut in scored] == [cut.score for cut in baseline]
        assert scored[0].metadata["logistic_model_id"] == "untrained_zero"
        assert scored[0].metadata["logistic_prob"] == 0.5
        assert scored[0].metadata["score_delta"] == 0.0
        candidates = [
            SimpleNamespace(start_cutpoint_id="short", end_cutpoint_id="long", weight=10.0),
        ]
        assert apply_candidate_weight_policy(candidates, baseline, O0_4)[0].weight == 10.0
        assert apply_candidate_weight_policy(candidates, scored, O0_4_LOGISTIC)[0].weight == 10.0

    def test_fitted_model_changes_scores_and_packer_weights(self) -> None:
        x_rows, y_rows = _separable_training()
        spec = fit_logistic(x_rows, y_rows, model_id="synthetic_fit")
        config = replace(O0_4_LOGISTIC, logistic=spec)
        cuts = [
            _cut(
                cut_id="short",
                time_sec=2.0,
                quiet_ms=24.0,
                score=1.0,
                rms_dbfs=-20.0,
                prominence_db=0.0,
            ),
            _cut(
                cut_id="long",
                time_sec=8.0,
                quiet_ms=240.0,
                score=1.0,
                rms_dbfs=-55.0,
                prominence_db=8.0,
            ),
        ]
        scored = apply_cut_policy(cuts, config)
        assert scored[1].score > scored[0].score
        assert scored[1].metadata["logistic_prob"] > scored[0].metadata["logistic_prob"]
        candidates = [
            SimpleNamespace(start_cutpoint_id="short", end_cutpoint_id="long", weight=10.0),
        ]
        weighted = apply_candidate_weight_policy(candidates, scored, config)
        unchanged = apply_candidate_weight_policy(candidates, scored, O0_4)
        assert unchanged[0].weight == 10.0
        expected_delta = (scored[0].score - 1.0) + (scored[1].score - 1.0)
        assert weighted[0].weight == pytest.approx(10.0 + expected_delta, abs=1e-9)

    def test_guards_reject_mismatched_specs(self) -> None:
        cuts = [_cut(cut_id="c", time_sec=1.0, quiet_ms=80.0)]
        missing = replace(O0_4, name="broken", scoring="logistic_boundary")
        with pytest.raises(RuntimeError, match="no logistic spec"):
            apply_cut_policy(cuts, missing)
        dangling = replace(O0_4, name="broken", logistic=LOGISTIC_BOUNDARY_V1_UNTRAINED)
        with pytest.raises(RuntimeError, match="logistic spec but scoring"):
            apply_cut_policy(cuts, dangling)
        with pytest.raises(RuntimeError, match="unknown scoring policy"):
            apply_cut_policy(cuts, replace(O0_4, name="broken", scoring="not_a_policy"))
        bad_names = LogisticSpec(
            kind="boundary_v1",
            model_id="bad",
            feature_names=("nope",),
            weights=(0.0,),
        )
        with pytest.raises(RuntimeError, match="feature_names"):
            require_logistic_spec(replace(O0_4_LOGISTIC, logistic=bad_names))
        with pytest.raises(RuntimeError, match="not logistic_boundary"):
            apply_logistic_cut_policy(cuts, O0_4)
