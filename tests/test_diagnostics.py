"""Fast tests for geometry fingerprints and speaker_ts_eval src resolution."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from adapter.canonical_executor import execute_canonical
from adapter.config import CURRENT_A, PROPER_D, GeometryConfig
from adapter.diagnostics import (
    SPEAKER_TS_EVAL_SRC_ENV,
    geometry_canonical_payload,
    geometry_fingerprint,
    resolve_speaker_ts_eval_src,
)


class TestSpeakerTsEvalSrcResolution:
    def test_env_var_directory_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        src = tmp_path / "custom_src"
        src.mkdir()
        monkeypatch.setenv(SPEAKER_TS_EVAL_SRC_ENV, str(src))
        assert resolve_speaker_ts_eval_src() == src.resolve()

    def test_env_var_missing_directory_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv(SPEAKER_TS_EVAL_SRC_ENV, str(missing))
        with pytest.raises(RuntimeError, match=SPEAKER_TS_EVAL_SRC_ENV):
            resolve_speaker_ts_eval_src()

    def test_known_local_default_used_when_present(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(SPEAKER_TS_EVAL_SRC_ENV, raising=False)
        default = tmp_path / "speaker_ts_eval" / "src"
        default.mkdir(parents=True)
        monkeypatch.setattr(
            "adapter.diagnostics.known_local_speaker_ts_eval_src",
            lambda: default,
        )
        assert resolve_speaker_ts_eval_src() == default.resolve()

    def test_neither_env_nor_default_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(SPEAKER_TS_EVAL_SRC_ENV, raising=False)
        missing = tmp_path / "missing_src"
        monkeypatch.setattr(
            "adapter.diagnostics.known_local_speaker_ts_eval_src",
            lambda: missing,
        )
        with pytest.raises(RuntimeError, match=SPEAKER_TS_EVAL_SRC_ENV):
            resolve_speaker_ts_eval_src()

    def test_executor_does_not_hardcode_unconditional_developer_path(self) -> None:
        text = Path(execute_canonical.__code__.co_filename).read_text(encoding="utf-8")
        assert "resolve_speaker_ts_eval_src" in text
        assert '_SPEAKER_TS_EVAL_SRC = Path("/home/' not in text
        # Default is sibling-relative, not a username path constant.
        assert "known_local_speaker_ts_eval_src" in Path(
            resolve_speaker_ts_eval_src.__code__.co_filename
        ).read_text(encoding="utf-8")


class TestGeometryFingerprint:
    def test_a_and_d_fingerprints_differ(self) -> None:
        assert geometry_fingerprint(CURRENT_A) != geometry_fingerprint(PROPER_D)
        assert geometry_canonical_payload(CURRENT_A)["hop_samples"] == 256
        assert geometry_canonical_payload(PROPER_D)["hop_samples"] == 512
        assert geometry_canonical_payload(CURRENT_A)["offsets"] == [0, 128]
        assert geometry_canonical_payload(PROPER_D)["offsets"] == [0, 128, 256, 384]

    def test_same_config_is_stable(self) -> None:
        first = geometry_fingerprint(CURRENT_A)
        second = geometry_fingerprint(CURRENT_A)
        assert first == second
        clone = GeometryConfig(
            name="alias_A",
            window_samples=CURRENT_A.window_samples,
            hop_samples=CURRENT_A.hop_samples,
            offsets=CURRENT_A.offsets,
            sample_rate_hz=CURRENT_A.sample_rate_hz,
            vad_backend=CURRENT_A.vad_backend,
        )
        assert geometry_fingerprint(clone) == first

    def test_fingerprint_ignores_packer_and_scoring_knobs(self) -> None:
        mutated = replace(
            CURRENT_A,
            name="other",
            min_clip_sec=99.0,
            preferred_min_sec=1.0,
            vad_threshold=0.9,
            percentile_rms_percentile=50.0,
        )
        assert geometry_fingerprint(mutated) == geometry_fingerprint(CURRENT_A)
