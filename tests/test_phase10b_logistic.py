"""Phase 10B: speaker-held-out OOF logistic on the frozen 15-feature table."""

from __future__ import annotations

import inspect
import math
from types import SimpleNamespace

import pytest

from adapter.config import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_ID,
    O0_4,
    PHASE10B_FEATURE_SETS,
    PHASE10B_TARGETS,
    RMS_WAVEFORM,
    VAD_ONLY,
)
from adapter.contender_policy import apply_candidate_weight_policy
from adapter.diagnostics import geometry_fingerprint
from adapter.logistic_cv import (
    fit_logistic_spec,
    fold_state_counts,
    model_name,
    oof_predictions,
    parse_model_name,
    speaker_held_out_folds,
    train_fold_mean_scale,
)
from adapter.logistic_extract import extract_recording_candidates
from adapter.logistic_features import features_from_cut
from adapter.logistic_labels import (
    LABEL_SCHEMA_ID,
    STATE_BAD,
    STATE_SAFE,
    STATE_UNKNOWN,
    count_label_states,
    label_candidate,
)
from adapter.logistic_policy import apply_precomputed_p_bad
from adapter.logistic_experiment import CONTENDER_NAMES, load_candidates_if_current, schedule_diffs_vs_o0_4
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
        score=score,
        metadata={"original_score": score},
    )


def _candidate(
    *,
    speaker: str,
    candidate_id: str,
    y_inside: int | None = 0,
    y_gt20ms: int | None = 0,
    y_gt50ms: int | None = 0,
    state_inside: str | None = None,
    state_gt20ms: str | None = None,
    state_gt50ms: str | None = None,
    **features: float,
) -> dict[str, object]:
    def _state(explicit: str | None, y: int | None) -> str:
        if explicit is not None:
            return explicit
        if y == 1:
            return STATE_BAD
        if y == 0:
            return STATE_SAFE
        return STATE_UNKNOWN

    row: dict[str, object] = {name: 0.0 for name in FEATURE_NAMES}
    row.update(features)
    row.update(
        {
            "speaker_id": speaker,
            "recording_id": f"{speaker}a",
            "candidate_id": candidate_id,
            "cutpoint_id": candidate_id,
            "state_inside": _state(state_inside, y_inside),
            "state_gt20ms": _state(state_gt20ms, y_gt20ms),
            "state_gt50ms": _state(state_gt50ms, y_gt50ms),
            "y_inside": y_inside,
            "y_gt20ms": y_gt20ms,
            "y_gt50ms": y_gt50ms,
        }
    )
    return row


def test_feature_schema_is_frozen() -> None:
    assert FEATURE_SCHEMA_ID == "o0_4_boundary_features_v1"
    assert LABEL_SCHEMA_ID == "phase10b_training_labels_v1"
    assert len(FEATURE_NAMES) == 15
    assert FEATURE_NAMES[:13] == RMS_WAVEFORM
    assert FEATURE_NAMES[13:] == VAD_ONLY
    assert geometry_fingerprint(O0_4) == TRUSTED_O0_4


def test_nine_models_cover_subsets_and_targets() -> None:
    names = [model_name(subset, target) for subset in PHASE10B_FEATURE_SETS for target in PHASE10B_TARGETS]
    assert len(names) == 9
    assert len(set(names)) == 9
    assert CONTENDER_NAMES[0] == "O0_4"
    assert tuple(CONTENDER_NAMES[1:]) == tuple(names)


def test_labels_use_strict_inside_and_trusted_pause() -> None:
    phones = [_phone(1.0, 1.1), _phone(1.3, 1.4)]
    reference = _reference(phones)
    endpoint = label_candidate(1.0, recording_id="rec", buffer_id="buf0", reference=reference)
    assert endpoint["state_inside"] == STATE_UNKNOWN
    assert endpoint["y_inside"] is None
    other_end = label_candidate(1.1, recording_id="rec", buffer_id="buf0", reference=reference)
    assert other_end["state_inside"] == STATE_UNKNOWN
    near_gap = label_candidate(1.11, recording_id="rec", buffer_id="buf0", reference=reference)
    assert near_gap["state_inside"] == STATE_UNKNOWN
    margin = label_candidate(1.12, recording_id="rec", buffer_id="buf0", reference=reference)
    assert margin["state_inside"] == STATE_SAFE
    assert margin["y_inside"] == 0
    shallow = label_candidate(1.01, recording_id="rec", buffer_id="buf0", reference=reference)
    assert shallow["state_inside"] == STATE_BAD
    assert shallow["y_inside"] == 1
    assert shallow["state_gt20ms"] == STATE_UNKNOWN
    assert shallow["y_gt20ms"] is None
    mid = label_candidate(1.03, recording_id="rec", buffer_id="buf0", reference=reference)
    assert mid["state_inside"] == STATE_BAD
    assert mid["state_gt20ms"] == STATE_BAD
    assert mid["state_gt50ms"] == STATE_UNKNOWN
    exact_50 = label_candidate(1.05, recording_id="rec", buffer_id="buf0", reference=reference)
    assert exact_50["state_gt50ms"] == STATE_UNKNOWN
    deep_ref = _reference([_phone(1.0, 1.2)])
    deep = label_candidate(1.1, recording_id="rec", buffer_id="buf0", reference=deep_ref)
    assert deep["state_gt50ms"] == STATE_BAD
    assert deep["y_gt50ms"] == 1
    pause = label_candidate(1.2, recording_id="rec", buffer_id="buf0", reference=reference)
    assert pause["state_inside"] == STATE_SAFE
    assert pause["state_gt20ms"] == STATE_SAFE
    assert pause["y_inside"] == 0
    outside = label_candidate(0.5, recording_id="rec", buffer_id="buf0", reference=reference)
    assert outside["state_inside"] == STATE_UNKNOWN
    assert outside["y_inside"] is None
    masked = label_candidate(
        1.2,
        recording_id="rec",
        buffer_id="buf0",
        reference=_reference(phones, [_unc(1.15, 1.25)]),
    )
    assert masked["state_inside"] == STATE_UNKNOWN
    counts = count_label_states(
        [
            {"state_inside": STATE_BAD, "state_gt20ms": STATE_SAFE, "state_gt50ms": STATE_UNKNOWN},
            {"state_inside": STATE_SAFE, "state_gt20ms": STATE_SAFE, "state_gt50ms": STATE_SAFE},
        ]
    )
    assert counts["inside"] == {STATE_BAD: 1, STATE_SAFE: 1, STATE_UNKNOWN: 0}


def test_stale_candidates_csv_without_label_schema_is_ignored(tmp_path) -> None:
    path = tmp_path / "candidates.csv"
    path.write_text("schema_id,speaker_id,recording_id\no0_4_boundary_features_v1,s01,s0101b\n")
    assert load_candidates_if_current(path) is None


def test_speaker_folds_hold_out_whole_speakers() -> None:
    speakers = ["s01", "s02", "s03", "s04", "s05"]
    folds = speaker_held_out_folds(speakers, n_splits=5)
    assert len(folds) == 5
    seen: list[str] = []
    for train, test in folds:
        assert not set(train) & set(test)
        seen.extend(test)
        for speaker in test:
            assert speaker not in train
    assert sorted(seen) == speakers


def test_mean_scale_uses_training_fold_only() -> None:
    train = [(0.0, 2.0), (2.0, 4.0)]
    mean, scale = train_fold_mean_scale(train)
    assert mean == pytest.approx((1.0, 3.0))
    # population std of [0, 2] is 1.0
    assert scale == pytest.approx((1.0, 1.0))
    leaked = train_fold_mean_scale(train + [(100.0, 100.0)])
    assert leaked[0] != mean


def test_constant_fold_does_not_need_sklearn() -> None:
    features = [(0.1, 0.2), (0.3, 0.4)]
    spec = fit_logistic_spec(
        features,
        [0, 0],
        subset="vad_only",
        model_id="constant",
    )
    assert spec.coefficients == (0.0, 0.0)
    from adapter.logistic_model import predict_p_bad

    assert predict_p_bad((0.1, 0.2), spec) == pytest.approx(0.0, abs=1e-9)


def test_oof_predicts_each_candidate_once() -> None:
    rows = [
        _candidate(speaker="s01", candidate_id="a", vad_mean_64ms=0.1),
        _candidate(speaker="s02", candidate_id="b", vad_mean_64ms=0.2),
        _candidate(speaker="s03", candidate_id="c", vad_mean_64ms=0.3),
        _candidate(speaker="s04", candidate_id="d", vad_mean_64ms=0.4),
    ]
    predicted, specs = oof_predictions(rows, subset="vad_only", target="inside", n_splits=2)
    assert set(predicted) == {"a", "b", "c", "d"}
    assert len(specs) == 2
    assert all(0.0 <= value <= 1.0 for value in predicted.values())


def test_higher_oof_p_bad_lowers_packer_weight() -> None:
    cuts = [_cut(cut_id="start", score=1.0), _cut(cut_id="end", score=1.0)]
    mixed = apply_precomputed_p_bad(cuts, {"start": 0.9, "end": 0.1}, model_id="demo")
    assert mixed[0].score < 1.0
    assert mixed[1].score > 1.0
    bad = apply_precomputed_p_bad(cuts, {"start": 0.9, "end": 0.9}, model_id="demo")
    candidates = [
        SimpleNamespace(
            start_cutpoint_id="start",
            end_cutpoint_id="end",
            weight=1.0,
        )
    ]
    config = SimpleNamespace(scoring="logistic_boundary", rms_policy=None)
    weighted = apply_candidate_weight_policy(candidates, bad, config)  # type: ignore[arg-type]
    assert weighted[0].weight < 1.0


def test_features_extracted_before_labels_and_without_phones() -> None:
    source = inspect.getsource(extract_recording_candidates)
    assert source.index("features_from_cut") < source.index("label_candidate")
    feature_source = inspect.getsource(features_from_cut)
    assert "phone" not in feature_source.lower()
    import adapter.logistic_extract as extract_mod

    module_source = inspect.getsource(extract_mod)
    assert "SlicerRequest" in module_source
    assert "loaded.reference" in source


def test_sklearn_oof_ranks_the_separating_feature() -> None:
    pytest.importorskip("sklearn")
    pytest.importorskip("numpy")
    rows = []
    for speaker_index, speaker in enumerate(("s01", "s02", "s03", "s04")):
        for copy in range(4):
            inside = copy % 2
            rows.append(
                _candidate(
                    speaker=speaker,
                    candidate_id=f"{speaker}-{copy}",
                    y_inside=inside,
                    vad_mean_64ms=0.9 if inside else 0.1,
                    low_vad_run_width=float(speaker_index),
                )
            )
    predicted, specs = oof_predictions(rows, subset="vad_only", target="inside", n_splits=2)
    inside_p = [predicted[row["candidate_id"]] for row in rows if row["y_inside"] == 1]
    outside_p = [predicted[row["candidate_id"]] for row in rows if row["y_inside"] == 0]
    assert sum(inside_p) / len(inside_p) > sum(outside_p) / len(outside_p)
    assert all(math.isfinite(value) for value in predicted.values())
    assert specs[0].feature_subset == "vad_only"


def test_unknown_rows_are_excluded_from_fit_but_still_predicted() -> None:
    rows = [
        _candidate(speaker="s01", candidate_id="s01-k", y_inside=0, vad_mean_64ms=1.0),
        _candidate(
            speaker="s01",
            candidate_id="s01-u",
            state_inside=STATE_UNKNOWN,
            y_inside=None,
            vad_mean_64ms=100.0,
        ),
        _candidate(speaker="s02", candidate_id="s02-k", y_inside=0, vad_mean_64ms=3.0),
        _candidate(speaker="s03", candidate_id="s03-k", y_inside=0, vad_mean_64ms=1.0),
        _candidate(
            speaker="s03",
            candidate_id="s03-u",
            state_inside=STATE_UNKNOWN,
            y_inside=None,
            vad_mean_64ms=100.0,
        ),
        _candidate(speaker="s04", candidate_id="s04-k", y_inside=0, vad_mean_64ms=3.0),
    ]
    predicted, specs = oof_predictions(rows, subset="vad_only", target="inside", n_splits=2)
    assert set(predicted) == {row["candidate_id"] for row in rows}
    assert specs[0].feature_mean[0] == pytest.approx(3.0)
    assert specs[1].feature_mean[0] == pytest.approx(1.0)


def test_parse_model_name_and_fold_counts() -> None:
    assert parse_model_name("O0_4_LR_rms_waveform_gt20ms") == ("rms_waveform", "gt20ms")
    assert parse_model_name("O0_4_LR_all_inside") == ("all", "inside")
    rows = [
        _candidate(speaker="s01", candidate_id="a", y_inside=1),
        _candidate(speaker="s02", candidate_id="b", y_inside=0),
        _candidate(
            speaker="s03",
            candidate_id="c",
            state_inside=STATE_UNKNOWN,
            y_inside=None,
        ),
        _candidate(speaker="s04", candidate_id="d", y_inside=0),
    ]
    folds = fold_state_counts(rows, target="inside", n_splits=2)
    assert folds[0]["test_bad"] + folds[1]["test_bad"] == 1
    assert folds[0]["test_unknown"] + folds[1]["test_unknown"] == 1


def test_schedule_diffs_count_hash_changes() -> None:
    rows = [
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "geometry": "O0_4",
            "selected_cutpoint_sha256": "aaa",
            "selected_clip_sha256": "bbb",
        },
        {
            "speaker_id": "s01",
            "recording_id": "r1",
            "geometry": "O0_4_LR_all_inside",
            "selected_cutpoint_sha256": "ccc",
            "selected_clip_sha256": "bbb",
        },
    ]
    diffs = schedule_diffs_vs_o0_4(rows)
    assert diffs["O0_4_LR_all_inside"]["cut_hash_diff"] == 1
    assert diffs["O0_4_LR_all_inside"]["clip_hash_diff"] == 0
    assert diffs["O0_4_LR_all_inside"]["either_diff"] == 1


def test_reconstructed_o0_4_matches_canonical_schedule() -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("soundfile")
    from adapter.buckeye_loader import load_recording
    from adapter.canonical_executor import execute_canonical_diagnosed
    from adapter.diagnostics import hash_clip_intervals, hash_cutpoint_times
    from adapter.logistic_pack import pack_o0_4_baseline
    from adapter.types import SlicerRequest

    loaded = load_recording("s01", "s0101b")
    canonical = execute_canonical_diagnosed(
        SlicerRequest(
            recording_id=loaded.recording_id,
            audio_path=loaded.audio_path,
            sample_rate_hz=loaded.sample_rate_hz,
            buffers=loaded.buffers,
            config=O0_4,
        )
    )
    rows = extract_recording_candidates(loaded)
    reconstructed = pack_o0_4_baseline(rows)
    assert hash_cutpoint_times(reconstructed.cutpoints) == hash_cutpoint_times(canonical.raw.cutpoints)
    assert hash_clip_intervals(reconstructed.clips) == hash_clip_intervals(canonical.raw.clips)
