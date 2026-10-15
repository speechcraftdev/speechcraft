from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "runtime_probe.py"
SPEC = importlib.util.spec_from_file_location("speechcraft_runtime_probe", SCRIPT)
runtime_probe = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(runtime_probe)


def test_probe_audio_uses_integrity_pinned_bundled_fixture_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", raising=False)
    assert runtime_probe.audio_file("whisper").name == "speech.wav"
    assert runtime_probe.audio_file("silero").name == "speech-silence.wav"
    assert runtime_probe.audio_file("ffmpeg").name == "speech-compressed.mp3"


def test_probe_audio_rejects_symlinks_and_oversized_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "recording.wav"
    target.write_bytes(b"tiny")
    link = tmp_path / "linked.wav"
    link.symlink_to(target)
    monkeypatch.setenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", str(link))
    with pytest.raises(RuntimeError, match="missing, unsafe"):
        runtime_probe.audio_file("whisper")
    monkeypatch.setenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", str(target))
    assert runtime_probe.audio_file("whisper") == target


def test_probe_fixture_digest_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_root = tmp_path / "runtime-fixtures"
    fixture_root.mkdir()
    (fixture_root / "speech.wav").write_bytes(b"changed but same tiny file")
    monkeypatch.setattr(runtime_probe, "WORKER_ROOT", tmp_path)
    monkeypatch.delenv("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", raising=False)
    with pytest.raises(RuntimeError, match="failed its pinned integrity check"):
        runtime_probe.audio_file("whisper")


class AnnotationFixture:
    def __init__(self, rows):
        self.rows = rows

    def itertracks(self, *, yield_label=False):
        assert yield_label is True
        yield from self.rows


def annotation(*rows):
    return AnnotationFixture([(SimpleNamespace(start=start, end=end), None, label)
                              for start, end, label in rows])


def test_community1_probe_converts_pyannote_annotation_through_production_contract():
    rows, speakers = runtime_probe.community1_probe_regions(
        annotation((0.25, 1.5, "speaker_0"), (2.0, 3.25, "speaker_1")),
        sample_rate=16_000, recording_duration=4.0)
    assert len(rows) == 2
    assert speakers == {"speaker_0", "speaker_1"}
    assert rows[0]["backend"] == "pyannote_community_1"
    assert rows[0]["start_sample"] == 4_000


@pytest.mark.parametrize("value", [
    annotation(),
    annotation((0.0, 1.0, "speaker_0")),
    annotation((0.0, 1.0, "speaker_0"), (float("nan"), 2.0, "speaker_1")),
    annotation((0.0, 5.0, "speaker_0"), (1.0, 2.0, "speaker_1")),
    AnnotationFixture([(object(), None, "speaker_0")]),
])
def test_community1_probe_rejects_empty_invalid_or_single_speaker_annotations(value):
    with pytest.raises((RuntimeError, ValueError, TypeError, AttributeError)):
        runtime_probe.community1_probe_regions(value, sample_rate=16_000, recording_duration=4.0)


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
