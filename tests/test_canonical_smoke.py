"""Real canonical A/D smoke. Skipped unless numpy/torch/silero-vad are importable.

Run the dedicated script for the recorded integration command:

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_canonical_ad_smoke.py
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from adapter.config import CURRENT_A, PROPER_D
from adapter.diagnostics import geometry_fingerprint
from referee.types import SlicerResult

pytestmark = pytest.mark.canonical


def _have_canonical_runtime() -> bool:
    for name in ("numpy", "torch", "soundfile", "silero_vad"):
        try:
            __import__(name)
        except ImportError:
            return False
    return True


skip_without_runtime = pytest.mark.skipif(
    not _have_canonical_runtime(),
    reason="canonical A/D smoke needs numpy, torch, soundfile, and silero_vad",
)


@pytest.fixture(scope="module")
def smoke_wav(tmp_path_factory: pytest.TempPathFactory):
    if not _have_canonical_runtime():
        pytest.skip("canonical A/D smoke needs numpy, torch, soundfile, and silero_vad")
    from adapter.canonical_smoke import prepare_fixture_wav

    return prepare_fixture_wav(tmp_path_factory.mktemp("canonical_wav"))


@pytest.fixture(scope="module")
def smoke_runs(smoke_wav):
    if not _have_canonical_runtime():
        pytest.skip("canonical A/D smoke needs numpy, torch, soundfile, and silero_vad")
    from adapter.canonical_smoke import run_ad_order_smoke

    return run_ad_order_smoke(smoke_wav)


@skip_without_runtime
def test_real_canonical_a_execution_smoke(smoke_wav, smoke_runs) -> None:
    from adapter.canonical_smoke import assert_valid_public_result

    run = smoke_runs["A_in_AD"]
    assert run.diagnostics.geometry_name == CURRENT_A.name
    assert run.diagnostics.geometry_fingerprint == geometry_fingerprint(CURRENT_A)
    assert run.diagnostics.vad_observation_count > 0
    assert_valid_public_result(smoke_wav, run.result)


@skip_without_runtime
def test_real_canonical_d_execution_smoke(smoke_wav, smoke_runs) -> None:
    from adapter.canonical_smoke import assert_valid_public_result

    run = smoke_runs["D_in_AD"]
    assert run.diagnostics.geometry_name == PROPER_D.name
    assert run.diagnostics.geometry_fingerprint == geometry_fingerprint(PROPER_D)
    assert run.diagnostics.vad_observation_count > 0
    assert_valid_public_result(smoke_wav, run.result)


@skip_without_runtime
def test_ad_geometry_sensitive_diagnostic_regression(smoke_runs) -> None:
    from adapter.canonical_smoke import assert_a_d_not_collapsed

    assert_a_d_not_collapsed(
        smoke_runs["A_in_AD"].diagnostics,
        smoke_runs["D_in_AD"].diagnostics,
    )
    assert_a_d_not_collapsed(
        smoke_runs["A_in_DA"].diagnostics,
        smoke_runs["D_in_DA"].diagnostics,
    )


@skip_without_runtime
def test_ad_order_independence(smoke_runs) -> None:
    from adapter.canonical_smoke import assert_order_independence

    assert_order_independence(smoke_runs)


@skip_without_runtime
def test_no_shared_cache_or_workdir(smoke_runs) -> None:
    from adapter.canonical_smoke import assert_independent_state

    a = smoke_runs["A_in_AD"].diagnostics
    d = smoke_runs["D_in_AD"].diagnostics
    assert_independent_state(a, d)
    assert "current_A" in a.workdir
    assert "proper_D" in d.workdir
    assert a.feature_cache_dir != d.feature_cache_dir


@skip_without_runtime
def test_public_result_remains_neutral_slicer_result(smoke_runs) -> None:
    for run in smoke_runs.values():
        assert isinstance(run.result, SlicerResult)
        assert {f.name for f in fields(run.result)} == {"cutpoints", "clips"}
        assert not hasattr(run.result, "geometry_fingerprint")
        assert not hasattr(run.result, "diagnostics")
        assert not hasattr(run.result, "vad_observation_count")


@skip_without_runtime
def test_request_path_still_has_no_annotations() -> None:
    from adapter.types import SlicerRequest

    request_fields = {f.name for f in fields(SlicerRequest)}
    assert request_fields == {
        "recording_id",
        "audio_path",
        "sample_rate_hz",
        "buffers",
        "config",
    }
    for forbidden in (
        "phones",
        "uncertainty_intervals",
        "reference",
        "recording_reference",
        "words",
        "annotations",
    ):
        assert forbidden not in request_fields
