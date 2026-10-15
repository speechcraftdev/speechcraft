from __future__ import annotations

import pytest

from app.repository import _speechcraft_ffmpeg_executable


def test_managed_backend_requires_an_explicit_verified_ffmpeg_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEECHCRAFT_MANAGED_RUNTIME", "1")
    monkeypatch.delenv("SPEECHCRAFT_FFMPEG_BIN", raising=False)
    with pytest.raises(FileNotFoundError, match="no verified FFmpeg"):
        _speechcraft_ffmpeg_executable()

    binary = tmp_path / "ffmpeg"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("SPEECHCRAFT_FFMPEG_BIN", str(binary))
    assert _speechcraft_ffmpeg_executable() == str(binary)

    link = tmp_path / "ffmpeg-link"
    link.symlink_to(binary)
    monkeypatch.setenv("SPEECHCRAFT_FFMPEG_BIN", str(link))
    with pytest.raises(FileNotFoundError, match="missing or unsafe"):
        _speechcraft_ffmpeg_executable()
