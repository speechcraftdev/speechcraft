"""Phase 10C: signed-margin regression vs O0_4 score-control and pruning diagnostics."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from adapter.config import FEATURE_NAMES, FEATURE_SCHEMA_ID, O0_4, O0_4_MARGIN
from adapter.contender_policy import apply_candidate_weight_policy, apply_cut_policy
from adapter.diagnostics import geometry_fingerprint
from adapter.logistic_cv import speaker_held_out_folds
from adapter.margin_cv import oof_margin_predictions, oof_usable_regression_metrics
from adapter.margin_experiment import (
    CONTENDER_NAMES,
    PHASE10C_KEEP_FRACS,
    PHASE10C_SCORE_SCALES,
    PHASE10C_SCORE_WEIGHTS,
)
from adapter.margin_labels import (
    MARGIN_CAP_MS,
    MARGIN_REASONS,
    MARGIN_SCHEMA_ID,
    classify_margin,
    count_margin_reasons,
    signed_margin_ms,
)
from adapter.margin_model import preference_delta
from adapter.margin_pack import keep_top_score_fraction
from adapter.margin_policy import apply_precomputed_margin
from referee.types import BufferScope, PhoneInterval, RecordingReference, UncertaintyInterval


TRUSTED_O0_4 = "87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf"


def _phone(start: float, end: float, buffer_id: str = "buf0") -> PhoneInterval:
    return PhoneInterval(
        recording_id="rec",
        buffer_id=buffer_id,
        start_sec=start,
        end_sec=end,
        phone="AH",
    )


def _unc(start: float, end: float, buffer_id: str = "buf0") -> UncertaintyInterval:
    return UncertaintyInterval(
        recording_id="rec",
        buffer_id=buffer_id,
        start_sec=start,
        end_sec=end,
    )


def _reference(
    phones: list[PhoneInterval],
    masks: list[UncertaintyInterval] | None = None,
    *,
    start: float = 0.0,
    end: float = 10.0,
) -> RecordingReference:
    return RecordingReference(
        recording_id="rec",
        buffers=(BufferScope("buf0", start, end),),
        phones=tuple(phones),
        uncertainty_intervals=tuple(masks or ()),
    )


def _cut(*, cut_id: str, score: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        cutpoint_id=cut_id,
        recording_id="rec",
        buffer_id="buf0",
        time_sec=1.0,
        interval_start_sec=0.96,
        interval_end_sec=1.04,
        score=score,
        metadata={"original_score": score},
    )


def _candidate(
    *,
    speaker: str,
    candidate_id: str,
    y_margin_ms: int | None,
    **features: float,
) -> dict[str, object]:
    row: dict[str, object] = {name: 0.0 for name in FEATURE_NAMES}
    row.update(features)
    row.update(
        {
            "speaker_id": speaker,
            "recording_id": f"{speaker}a",
            "candidate_id": candidate_id,
            "cutpoint_id": candidate_id,
            "y_margin_ms": y_margin_ms,
            "margin_usable": 0 if y_margin_ms is None else 1,
        }
    )
    return row


def test_feature_and_margin_schemas_are_frozen() -> None:
    assert FEATURE_SCHEMA_ID == "o0_4_boundary_features_v1"
    assert MARGIN_SCHEMA_ID == "phase10c_signed_margin_v1"
    assert MARGIN_CAP_MS == 100
    assert geometry_fingerprint(O0_4) == TRUSTED_O0_4
    assert geometry_fingerprint(O0_4_MARGIN) == TRUSTED_O0_4
    assert O0_4.scoring is None
    assert O0_4.score_weight is None
    assert O0_4_MARGIN.scoring == "margin_regression"
    assert O0_4_MARGIN.logistic is None
    assert O0_4_MARGIN.score_weight is None


def test_signed_margin_trains_the_middle_not_only_extremes() -> None:
    phones = [_phone(1.0, 1.1), _phone(1.3, 1.4)]
    reference = _reference(phones)
    kwargs = {"recording_id": "rec", "buffer_id": "buf0", "reference": reference}
    assert signed_margin_ms(1.0, **kwargs) == 0
    assert signed_margin_ms(1.1, **kwargs) == 0
    assert signed_margin_ms(1.05, **kwargs) == -50
    assert signed_margin_ms(1.01, **kwargs) == -10
    assert signed_margin_ms(1.12, **kwargs) == 20
    assert signed_margin_ms(1.2, **kwargs) == 100
    deep_ref = _reference([_phone(1.0, 1.5)])
    assert signed_margin_ms(1.25, recording_id="rec", buffer_id="buf0", reference=deep_ref) == -100
    assert signed_margin_ms(0.5, **kwargs) is None
    masked = signed_margin_ms(
        1.2,
        recording_id="rec",
        buffer_id="buf0",
        reference=_reference(phones, [_unc(1.15, 1.25)]),
    )
    assert masked is None
    reasons = {
        0.5: "unknown_before_annotated_span",
        1.05: "usable_inside",
        1.0: "usable_edge",
        1.12: "usable_outside",
        9.0: "unknown_after_annotated_span",
    }
    for time_sec, expected in reasons.items():
        _margin, reason = classify_margin(time_sec, **kwargs)
        assert reason == expected
    empty = _reference([])
    _margin, reason = classify_margin(
        1.0, recording_id="rec", buffer_id="buf0", reference=empty
    )
    assert reason == "unknown_no_phones"
    _margin, reason = classify_margin(
        1.2,
        recording_id="rec",
        buffer_id="buf0",
        reference=_reference(phones, [_unc(1.15, 1.25)]),
    )
    assert reason == "unknown_uncertainty"
    _margin, reason = classify_margin(
        -1.0, recording_id="rec", buffer_id="buf0", reference=reference
    )
    assert reason == "unknown_outside_buffer"
    counted = count_margin_reasons(
        [{"margin_reason": reason} for reason in MARGIN_REASONS]
    )
    assert counted == {reason: 1 for reason in MARGIN_REASONS}


def test_signed_margin_caps_giant_pauses() -> None:
    reference = _reference([_phone(1.0, 1.1), _phone(2.0, 2.1)])
    kwargs = {"recording_id": "rec", "buffer_id": "buf0", "reference": reference}
    assert signed_margin_ms(1.5, **kwargs) == 100


def test_keep_fraction_is_per_buffer_and_keeps_highest_scores() -> None:
    rows = [
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "buffer_id": "buf0",
            "cutpoint_id": "a",
            "original_score": 1.0,
        },
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "buffer_id": "buf0",
            "cutpoint_id": "b",
            "original_score": 3.0,
        },
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "buffer_id": "buf0",
            "cutpoint_id": "c",
            "original_score": 2.0,
        },
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "buffer_id": "buf1",
            "cutpoint_id": "d",
            "original_score": 9.0,
        },
    ]
    kept = keep_top_score_fraction(rows, 0.5)
    ids = {str(row["cutpoint_id"]) for row in kept}
    assert ids == {"b", "c", "d"}
    assert {str(row["cutpoint_id"]) for row in keep_top_score_fraction(rows, 1.0)} == {
        "a",
        "b",
        "c",
        "d",
    }


def test_higher_predicted_margin_raises_packer_weight() -> None:
    cuts = [_cut(cut_id="start", score=1.0), _cut(cut_id="end", score=1.0)]
    mixed = apply_precomputed_margin(cuts, {"start": 80.0, "end": -80.0}, model_id="demo")
    assert mixed[0].score > 1.0
    assert mixed[1].score < 1.0
    assert preference_delta(100.0, score_scale=1.0, cap_ms=100.0) == pytest.approx(1.0)
    assert preference_delta(-100.0, score_scale=1.0, cap_ms=100.0) == pytest.approx(-1.0)
    safer = apply_precomputed_margin(cuts, {"start": 80.0, "end": 80.0}, model_id="demo")
    candidates = [
        SimpleNamespace(
            start_cutpoint_id="start",
            end_cutpoint_id="end",
            weight=1.0,
        )
    ]
    config = SimpleNamespace(scoring="margin_regression", rms_policy=None)
    weighted = apply_candidate_weight_policy(candidates, safer, config)  # type: ignore[arg-type]
    assert weighted[0].weight > 1.0


def test_live_cut_policy_refuses_margin_regression() -> None:
    with pytest.raises(RuntimeError, match="OOF-only"):
        apply_cut_policy([_cut(cut_id="c")], O0_4_MARGIN)


def test_detector_score_weight_preserves_candidates_and_uses_existing_scores() -> None:
    cuts = [_cut(cut_id="start", score=2.0), _cut(cut_id="end", score=3.0)]
    candidates = [
        SimpleNamespace(
            start_cutpoint_id="start",
            end_cutpoint_id="end",
            weight=10.0,
        )
    ]
    baseline = SimpleNamespace(scoring=None, rms_policy=None, score_weight=None)
    unchanged = apply_candidate_weight_policy(candidates, cuts, baseline)  # type: ignore[arg-type]
    assert unchanged[0].weight == 10.0
    zero = SimpleNamespace(scoring="o0_4_score_weight", rms_policy=None, score_weight=0.0)
    assert apply_candidate_weight_policy(candidates, cuts, zero)[0].weight == 10.0  # type: ignore[arg-type]
    weighted = apply_candidate_weight_policy(
        candidates,
        cuts,
        SimpleNamespace(scoring="o0_4_score_weight", rms_policy=None, score_weight=2.0),  # type: ignore[arg-type]
    )
    assert weighted[0].weight == pytest.approx(20.0)
    louder_start = [_cut(cut_id="start", score=4.0), _cut(cut_id="end", score=3.0)]
    louder = apply_candidate_weight_policy(
        candidates,
        louder_start,
        SimpleNamespace(scoring="o0_4_score_weight", rms_policy=None, score_weight=2.0),  # type: ignore[arg-type]
    )
    assert louder[0].weight > weighted[0].weight


def test_operating_points_are_frozen_before_results() -> None:
    assert PHASE10C_KEEP_FRACS == (0.9, 0.8, 0.7, 0.6, 0.5)
    assert PHASE10C_SCORE_WEIGHTS == (0.25, 0.5, 1.0, 2.0, 4.0)
    assert PHASE10C_SCORE_SCALES == (0.5, 1.0, 2.0, 4.0)
    assert CONTENDER_NAMES[0] == "O0_4"
    assert "O0_4_sw1" in CONTENDER_NAMES
    assert "O0_4_sw0p25" in CONTENDER_NAMES
    assert "O0_4_keep90" in CONTENDER_NAMES
    assert "O0_4_MARGIN_all_s1" in CONTENDER_NAMES
    assert "O0_4_MARGIN_all_s0p5" in CONTENDER_NAMES
    assert CONTENDER_NAMES.index("O0_4_sw1") < CONTENDER_NAMES.index("O0_4_keep90")


def test_unknown_rows_are_excluded_from_fit_but_still_predicted() -> None:
    pytest.importorskip("sklearn")
    pytest.importorskip("numpy")
    rows = [
        _candidate(speaker="s01", candidate_id="s01-k", y_margin_ms=20, vad_mean_64ms=1.0),
        _candidate(speaker="s01", candidate_id="s01-u", y_margin_ms=None, vad_mean_64ms=100.0),
        _candidate(speaker="s02", candidate_id="s02-k", y_margin_ms=-20, vad_mean_64ms=3.0),
        _candidate(speaker="s03", candidate_id="s03-k", y_margin_ms=20, vad_mean_64ms=1.0),
        _candidate(
            speaker="s03",
            candidate_id="s03-u",
            y_margin_ms=None,
            vad_mean_64ms=100.0,
        ),
        _candidate(speaker="s04", candidate_id="s04-k", y_margin_ms=-20, vad_mean_64ms=3.0),
    ]
    predicted, specs = oof_margin_predictions(rows, subset="vad_only", n_splits=2)
    assert set(predicted) == {row["candidate_id"] for row in rows}
    assert specs[0].feature_mean[0] == pytest.approx(3.0)
    assert specs[1].feature_mean[0] == pytest.approx(1.0)
    metrics = oof_usable_regression_metrics(rows, predicted)
    assert metrics["n_usable"] == 4


def test_sklearn_oof_tracks_the_separating_feature() -> None:
    pytest.importorskip("sklearn")
    pytest.importorskip("numpy")
    rows = []
    for speaker in ("s01", "s02", "s03", "s04"):
        for copy, margin in enumerate((-80, -20, 20, 80)):
            rows.append(
                _candidate(
                    speaker=speaker,
                    candidate_id=f"{speaker}-{copy}",
                    y_margin_ms=margin,
                    vad_mean_64ms=float(margin),
                    low_vad_run_width=float(copy),
                )
            )
    predicted, specs = oof_margin_predictions(rows, subset="vad_only", n_splits=2)
    usable = [row for row in rows if row["margin_usable"] == 1]
    paired = [(float(row["y_margin_ms"]), predicted[str(row["candidate_id"])]) for row in usable]
    true_pos = [pred for true, pred in paired if true > 0]
    true_neg = [pred for true, pred in paired if true < 0]
    assert sum(true_pos) / len(true_pos) > sum(true_neg) / len(true_neg)
    assert specs[0].feature_subset == "vad_only"
    folds = speaker_held_out_folds([str(row["speaker_id"]) for row in rows], n_splits=2)
    assert all(not set(train) & set(test) for train, test in folds)


def test_margin_labels_do_not_enter_feature_extraction() -> None:
    import adapter.logistic_extract as extract_mod
    import adapter.logistic_features as feature_mod

    extract_source = inspect.getsource(extract_mod)
    feature_source = inspect.getsource(feature_mod.features_from_cut)
    assert "signed_margin_ms" not in extract_source
    assert "phone" not in feature_source.lower()
    assert "FEATURE_SCHEMA_ID" in extract_source
