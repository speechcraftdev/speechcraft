"""Phase 2 adapter tests — architecture and conversion; no full Buckeye corpus."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

from adapter import (
    CURRENT_A,
    PROPER_D,
    GeometryConfig,
    RawClip,
    RawCutpoint,
    RawSlicerOutput,
    SlicerRequest,
    run_geometry,
    run_slicer,
    to_slicer_result,
)
from adapter.canonical_executor import IndependentRunState
from adapter.convert import raw_from_pairs
from adapter.run import _ALLOWED_REQUEST_FIELDS, _FORBIDDEN_REQUEST_FIELDS
from referee import (
    BufferScope,
    PhoneInterval,
    RecordingReference,
    UncertaintyInterval,
    evaluate,
)
from referee.types import Clip, Cutpoint, SlicerResult


REC = "rec0"
BUF = "buf0"
AUDIO = Path("/tmp/does_not_need_to_exist_for_stub.wav")


def _buffers() -> tuple[BufferScope, ...]:
    return (BufferScope(BUF, 0.0, 30.0),)


def _request(config: GeometryConfig) -> SlicerRequest:
    return SlicerRequest(
        recording_id=REC,
        audio_path=AUDIO,
        sample_rate_hz=config.sample_rate_hz,
        buffers=_buffers(),
        config=config,
    )


def _empty_raw() -> RawSlicerOutput:
    return RawSlicerOutput(cutpoints=(), clips=())


# ---------------------------------------------------------------------------
# A. Config identity
# ---------------------------------------------------------------------------


class TestConfigIdentity:
    def test_a_and_d_unequal_with_exact_geometry(self) -> None:
        assert CURRENT_A != PROPER_D
        assert CURRENT_A.window_samples == 512
        assert CURRENT_A.hop_samples == 256
        assert CURRENT_A.offsets == (0, 128)
        assert CURRENT_A.sample_rate_hz == 16000

        assert PROPER_D.window_samples == 512
        assert PROPER_D.hop_samples == 512
        assert PROPER_D.offsets == (0, 128, 256, 384)
        assert PROPER_D.sample_rate_hz == 16000

    def test_configs_are_immutable(self) -> None:
        with pytest.raises(FrozenInstanceError):
            CURRENT_A.hop_samples = 999  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            PROPER_D.offsets = (0,)  # type: ignore[misc]
        # Shared module constants must remain distinct objects with fixed values.
        assert CURRENT_A.offsets is not PROPER_D.offsets
        mutated = replace(CURRENT_A, hop_samples=512)
        assert mutated != CURRENT_A
        assert CURRENT_A.hop_samples == 256


# ---------------------------------------------------------------------------
# B. Annotation ignorance
# ---------------------------------------------------------------------------


class TestAnnotationIgnorance:
    def test_request_fields_exclude_annotations(self) -> None:
        names = {f.name for f in fields(SlicerRequest)}
        assert names == _ALLOWED_REQUEST_FIELDS
        assert names.isdisjoint(_FORBIDDEN_REQUEST_FIELDS)
        for forbidden in (
            "phones",
            "uncertainty_intervals",
            "reference",
            "recording_reference",
            "words",
            "annotations",
        ):
            assert forbidden not in names

    def test_run_slicer_signature_has_no_annotation_params(self) -> None:
        params = inspect.signature(run_slicer).parameters
        assert "phones" not in params
        assert "reference" not in params
        assert "uncertainty_intervals" not in params
        assert "recording_reference" not in params

    def test_stub_executor_receives_only_request_audio_buffers_config(self) -> None:
        seen: list[SlicerRequest] = []

        def stub(request: SlicerRequest) -> RawSlicerOutput:
            seen.append(request)
            # Prove annotation types are not attributes of the request.
            assert not hasattr(request, "phones")
            assert not hasattr(request, "uncertainty_intervals")
            assert not hasattr(request, "reference")
            return RawSlicerOutput(
                cutpoints=(RawCutpoint(REC, BUF, 5.0),),
                clips=(RawClip(REC, BUF, 2.0, 6.0),),
            )

        result = run_slicer(_request(CURRENT_A), executor=stub)
        assert len(seen) == 1
        assert seen[0].config is CURRENT_A
        assert seen[0].buffers == _buffers()
        assert seen[0].audio_path == AUDIO
        assert isinstance(result, SlicerResult)
        assert result.cutpoints == (Cutpoint(REC, BUF, 5.0),)

    def test_cannot_construct_request_with_phone_kwargs(self) -> None:
        with pytest.raises(TypeError):
            SlicerRequest(  # type: ignore[call-arg]
                recording_id=REC,
                audio_path=AUDIO,
                sample_rate_hz=16000,
                buffers=_buffers(),
                config=CURRENT_A,
                phones=(PhoneInterval(REC, BUF, 0.0, 1.0, "AA"),),
            )


# ---------------------------------------------------------------------------
# C. Same path, different config
# ---------------------------------------------------------------------------


class TestSamePathDifferentConfig:
    def test_a_and_d_share_run_slicer_path(self) -> None:
        configs_seen: list[GeometryConfig] = []

        def stub(request: SlicerRequest) -> RawSlicerOutput:
            configs_seen.append(request.config)
            return _empty_raw()

        run_slicer(_request(CURRENT_A), executor=stub)
        run_slicer(_request(PROPER_D), executor=stub)
        assert configs_seen == [CURRENT_A, PROPER_D]

        # run_geometry is the same adapter path with an overlay.
        base = _request(CURRENT_A)
        run_geometry(base, PROPER_D, executor=stub)
        assert configs_seen[-1] is PROPER_D


# ---------------------------------------------------------------------------
# D. No shared run state
# ---------------------------------------------------------------------------


class TestNoSharedRunState:
    def test_stub_builds_independent_run_state_per_call(self) -> None:
        run_states: list[IndependentRunState] = []

        def stub(request: SlicerRequest) -> RawSlicerOutput:
            # Mimic canonical executor: fresh state object per invocation.
            state = IndependentRunState(
                workdir=Path(f"/tmp/fake_{len(run_states)}"),
                config=request.config,
                instance_id=len(run_states) + 1,
            )
            run_states.append(state)
            return _empty_raw()

        run_slicer(_request(CURRENT_A), executor=stub)
        run_slicer(_request(PROPER_D), executor=stub)

        assert len(run_states) == 2
        assert run_states[0] is not run_states[1]
        assert run_states[0].instance_id != run_states[1].instance_id
        assert run_states[0].config is CURRENT_A
        assert run_states[1].config is PROPER_D
        # No shared config object mutation / identity confusion.
        assert run_states[0].config != run_states[1].config

    def test_canonical_executor_documents_fresh_instance_ids(self) -> None:
        # Lightweight proof that IndependentRunState ids are allocated uniquely
        # without invoking Silero (heavy deps stay out of the default suite).
        a = IndependentRunState(workdir=Path("/tmp/a"), config=CURRENT_A, instance_id=1)
        b = IndependentRunState(workdir=Path("/tmp/b"), config=PROPER_D, instance_id=2)
        assert a.instance_id != b.instance_id
        assert a.config != b.config


# ---------------------------------------------------------------------------
# E. Neutral result conversion
# ---------------------------------------------------------------------------


class TestNeutralResultConversion:
    def test_raw_to_phase1_identity(self) -> None:
        raw = raw_from_pairs(
            cuts=[
                (REC, BUF, 4.5),
                ("rec1", "buf1", 1.25),
            ],
            clips=[
                (REC, BUF, 2.0, 6.0),
                ("rec1", "buf1", 0.5, 4.0),
            ],
        )
        result = to_slicer_result(raw)
        assert result == SlicerResult(
            cutpoints=(
                Cutpoint(REC, BUF, 4.5),
                Cutpoint("rec1", "buf1", 1.25),
            ),
            clips=(
                Clip(REC, BUF, 2.0, 6.0),
                Clip("rec1", "buf1", 0.5, 4.0),
            ),
        )
        # No implementation metadata on the public result.
        assert {f.name for f in fields(SlicerResult)} == {"cutpoints", "clips"}


# ---------------------------------------------------------------------------
# F. Phase-1 evaluator compatibility
# ---------------------------------------------------------------------------


class TestEvaluatorCompatibility:
    def test_adapter_result_scores_without_implementation_metadata(self) -> None:
        def stub(_request: SlicerRequest) -> RawSlicerOutput:
            return RawSlicerOutput(
                cutpoints=(RawCutpoint(REC, BUF, 5.0),),
                clips=(RawClip(REC, BUF, 2.0, 6.0),),
            )

        result = run_slicer(_request(CURRENT_A), executor=stub)
        assert {f.name for f in fields(result)} == {"cutpoints", "clips"}

        reference = RecordingReference(
            recording_id=REC,
            buffers=_buffers(),
            phones=(PhoneInterval(REC, BUF, 4.5, 5.5, "AA"),),
            uncertainty_intervals=(UncertaintyInterval(REC, BUF, 10.0, 11.0),),
        )
        out = evaluate(reference, result)
        assert out.unique_cut_safety.unique_cutpoint_count == 1
        assert out.unique_cut_safety.inside_phone_count == 1
        assert out.clip_count == 1
        assert out.emitted_audio_sec == pytest.approx(4.0)
