from __future__ import annotations

from pathlib import Path

import pytest

from speechcraft_dataset.runtime_tools import executable, verify_managed_model


def test_managed_tool_requires_explicit_executable_and_rejects_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECHCRAFT_MANAGED_RUNTIME", "1")
    monkeypatch.delenv("SPEECHCRAFT_FFMPEG_BIN", raising=False)
    with pytest.raises(FileNotFoundError, match="no verified ffmpeg"):
        executable("ffmpeg")

    actual = tmp_path / "ffmpeg"
    actual.write_text("#!/bin/sh\nexit 0\n")
    actual.chmod(0o700)
    monkeypatch.setenv("SPEECHCRAFT_FFMPEG_BIN", str(actual))
    assert executable("ffmpeg") == str(actual)

    link = tmp_path / "ffprobe"
    link.symlink_to(actual)
    monkeypatch.setenv("SPEECHCRAFT_FFPROBE_BIN", str(link))
    with pytest.raises(FileNotFoundError, match="missing or unsafe"):
        executable("ffprobe")


def test_development_tool_lookup_still_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECHCRAFT_MANAGED_RUNTIME", raising=False)
    monkeypatch.setenv("SPEECHCRAFT_FFMPEG_BIN", "/explicit/dev/ffmpeg")
    assert executable("ffmpeg") == "/explicit/dev/ffmpeg"
    with pytest.raises(ValueError, match="Unsupported runtime tool"):
        executable("arbitrary")


def test_model_verification_uses_catalog_and_rejects_nonverified_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from speechcraft_models import manifest

    calls: list[object] = []
    monkeypatch.setattr(manifest, "load_catalog", lambda: {"models": []})
    monkeypatch.setattr(manifest, "select_artifact", lambda catalog, role, platform: calls.append((role, platform)) or {"role": role})
    monkeypatch.setattr(manifest, "verify_artifact", lambda directory, artifact: {"status": "verified", "integrity_verified": True, "install_ready": True})
    assert verify_managed_model("speech_detection", tmp_path, "linux-x86_64")["status"] == "verified"
    assert calls == [("speech_detection", "linux-x86_64")]

    monkeypatch.setattr(manifest, "verify_artifact", lambda directory, artifact: {"status": "corrupt", "integrity_verified": False, "install_ready": False})
    with pytest.raises(RuntimeError, match="failed manifest integrity"):
        verify_managed_model("asr", tmp_path, "linux-x86_64")
    with pytest.raises(ValueError, match="Unsupported managed model role"):
        verify_managed_model("diarization", tmp_path, "linux-x86_64")
