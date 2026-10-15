from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "runtime_probe.py"
SPEC = importlib.util.spec_from_file_location("speechcraft_runtime_probe", SCRIPT)
runtime_probe = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime_probe)


def test_probe_audio_requires_explicit_local_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", raising=False)
    with pytest.raises(RuntimeError, match="local intelligible probe recording is required"):
        runtime_probe.audio_file()


def test_probe_audio_rejects_symlinks_and_oversized_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "recording.wav"
    target.write_bytes(b"tiny")
    link = tmp_path / "linked.wav"
    link.symlink_to(target)
    monkeypatch.setenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", str(link))
    with pytest.raises(RuntimeError, match="missing, unsafe"):
        runtime_probe.audio_file()
    monkeypatch.setenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", str(target))
    assert runtime_probe.audio_file() == target


def test_cli_emits_one_safe_json_blocker_for_missing_managed_ffmpeg(tmp_path: Path) -> None:
    recording = tmp_path / "probe.wav"
    recording.write_bytes(b"local fixture")
    environment = {key: value for key, value in os.environ.items() if key.startswith("PATH") or key.startswith("LANG")}
    environment.update({"PYTHONNOUSERSITE": "1", "SPEECHCRAFT_MANAGED_RUNTIME": "1",
                       "SPEECHCRAFT_RUNTIME_PROBE_AUDIO": str(recording),
                       "PYTHONPATH": os.pathsep.join(item for item in sys.path if item)})
    process = subprocess.run([sys.executable, str(SCRIPT), "ffmpeg"], env=environment,
                             capture_output=True, text=True, timeout=30)
    assert process.returncode != 0
    report = json.loads(process.stdout)
    assert report["schema_version"] == 1
    assert report["component"] == "ffmpeg"
    assert report["success"] is False
    assert report["error_code"] == "runtime_tool_unavailable"
    assert str(tmp_path) not in process.stdout
