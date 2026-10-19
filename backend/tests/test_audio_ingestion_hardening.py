from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from collections import namedtuple

import pytest

from app.audio_ingestion import CHUNK_SIZE, AudioUploadManager, UploadError, file_identity_hash
from app.audio_normalization import inspect_and_normalize, validate_decoded_sample_count
from app import audio_normalization
from app.models import ImportBatchCreate, SourceRecordingCreate
from app.repository import SQLiteRepository


def manifest(content: bytes) -> tuple[list[str], str]:
    chunks = [content[index : index + 8 * 1024 * 1024] for index in range(0, len(content), 8 * 1024 * 1024)]
    hashes = [hashlib.sha256(chunk).hexdigest() for chunk in chunks]
    return hashes, file_identity_hash(len(content), hashes)


def test_decoded_sample_count_rejects_material_truncation() -> None:
    sample_rate = 16_000
    expected_duration = 50 * 60
    with pytest.raises(UploadError, match="sample count"):
        validate_decoded_sample_count(expected_duration * sample_rate - 60 * sample_rate, sample_rate, expected_duration)

    validate_decoded_sample_count(expected_duration * sample_rate - sample_rate // 10, sample_rate, expected_duration)


def test_conversion_cleans_partial_output_and_can_retry_after_disk_full(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")
    output = tmp_path / "working.wav"
    calls = {"ffmpeg": 0}

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "ffprobe":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"streams": [{"codec_name": "mp3", "sample_rate": "16000", "channels": 1, "duration": "0.5"}]}),
                stderr="",
            )
        calls["ffmpeg"] += 1
        target = Path(command[-1])
        if calls["ffmpeg"] == 1:
            target.write_bytes(b"partial output")
            return SimpleNamespace(returncode=1, stdout="", stderr="No space left on device")
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16_000)
            audio.writeframes(b"\0\0" * 8_000)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    usage = SimpleNamespace(free=1024 * 1024 * 1024)
    with patch.object(audio_normalization, "_tool", side_effect=lambda name: name), patch.object(
        audio_normalization.subprocess, "run", side_effect=run
    ), patch.object(audio_normalization.shutil, "disk_usage", return_value=usage):
        with pytest.raises(OSError, match="Insufficient disk space"):
            inspect_and_normalize(source, output)
        assert not output.exists()
        result = inspect_and_normalize(source, output)
    assert result["converted"] is True
    assert output.is_file()


def test_upload_requires_peak_staging_space_before_session_creation(tmp_path: Path) -> None:
    manager = AudioUploadManager(tmp_path / "media")
    usage = namedtuple("usage", "total used free")
    hashes, identity = manifest(b"test")
    with patch("app.audio_ingestion.shutil.disk_usage", return_value=usage(1000, 900, 100)):
        with pytest.raises(UploadError, match="free disk space"):
            manager.create("project", "clip.wav", 4, None, identity, hashes)


def test_multiple_chunks_assemble_to_the_expected_file(tmp_path: Path) -> None:
    content = b"a" * CHUNK_SIZE + b"tail"
    hashes = [hashlib.sha256(content[:CHUNK_SIZE]).hexdigest(), hashlib.sha256(content[CHUNK_SIZE:]).hexdigest()]
    identity = file_identity_hash(len(content), hashes)
    manager = AudioUploadManager(tmp_path / "media")
    session = manager.create("project", "multi.wav", len(content), None, identity, hashes)
    manager.put_chunk(session["upload_id"], 0, content[:CHUNK_SIZE], hashes[0])
    manager.put_chunk(session["upload_id"], 1, content[CHUNK_SIZE:], hashes[1])
    assembled, assembled_hash = manager.assemble(session["upload_id"], tmp_path / "assembled.wav")
    assert assembled["file_identity_sha256"] == identity
    assert assembled_hash == hashlib.sha256(content).hexdigest()
    assert (tmp_path / "assembled.wav").read_bytes() == content


@pytest.mark.parametrize(
    ("extension", "codec"),
    [
        ("mp3", "libmp3lame"),
        ("flac", "flac"),
        ("m4a", "aac"),
        ("aac", "aac"),
        ("ogg", "libvorbis"),
        ("opus", "libopus"),
        ("aiff", "pcm_s16be"),
    ],
)
def test_supported_audio_codecs_normalize_to_pcm16(
    tmp_path: Path, extension: str, codec: str
) -> None:
    ffmpeg = os.environ.get("SPEECHCRAFT_FFMPEG_BIN") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("SPEECHCRAFT_FFPROBE_BIN") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and FFprobe are required for codec integration coverage")
    source = tmp_path / f"source.{extension}"
    output = tmp_path / "normalized.wav"
    subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=16000:duration=0.5", "-ac", "1", "-c:a", codec, str(source)],
        check=True, capture_output=True,
    )
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = inspect_and_normalize(source, output)
    with wave.open(str(output), "rb") as audio:
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == metadata["sample_rate"]
        assert audio.getnchannels() == 1
        validate_decoded_sample_count(audio.getnframes(), audio.getframerate(), metadata["duration_seconds"])
    assert metadata["converted"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash


def test_promotion_cleanup_only_removes_marker_owned_uncommitted_files(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    source_root = media_root / "sources"
    source_root.mkdir(parents=True)
    manager = AudioUploadManager(media_root)
    repository = SQLiteRepository(
        db_path=tmp_path / "project.db",
        legacy_seed_path=tmp_path / "missing-seed.json",
        media_root=media_root,
        exports_root=tmp_path / "exports",
    )
    try:
        unowned = source_root / "manually-managed.wav"
        unowned.write_bytes(b"keep")

        orphan_id = "source-" + "1" * 32
        orphan_upload = "a" * 32
        orphan_names = [f"{orphan_id}.wav", f"{orphan_id}.original.mp3"]
        for name in orphan_names:
            (source_root / name).write_bytes(b"orphan")
        manager.write_promotion_marker(orphan_upload, orphan_id, orphan_names)

        committed_id = "source-" + "2" * 32
        committed_upload = "b" * 32
        committed_path = source_root / f"{committed_id}.wav"
        with wave.open(str(committed_path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\0\0" * 16)
        repository.create_import_batch(ImportBatchCreate(id="ingest-test", name="Ingest Test"))
        repository.create_source_recording(
            SourceRecordingCreate(
                id=committed_id,
                batch_id="ingest-test",
                file_path=str(committed_path),
                sample_rate=16_000,
                num_channels=1,
                num_samples=16,
            ),
            artifact_metadata={"upload_session_id": committed_upload},
        )
        manager.write_promotion_marker(committed_upload, committed_id, [committed_path.name])

        removed = repository.cleanup_orphan_source_files(manager.recovery_root)
        assert removed == 2
        assert not any((source_root / name).exists() for name in orphan_names)
        assert unowned.read_bytes() == b"keep"
        assert committed_path.exists()
        assert not (manager.recovery_root / f"{orphan_upload}.json").exists()
        assert not (manager.recovery_root / f"{committed_upload}.json").exists()
    finally:
        repository.close()


def test_finalization_bookkeeping_failure_keeps_committed_audio(tmp_path: Path) -> None:
    from app import main

    media_root = tmp_path / "media"
    manager = AudioUploadManager(media_root)
    repository = SQLiteRepository(
        db_path=tmp_path / "project.db",
        legacy_seed_path=tmp_path / "missing-seed.json",
        media_root=media_root,
        exports_root=tmp_path / "exports",
    )
    try:
        repository.create_import_batch(ImportBatchCreate(id="commit-test", name="Commit Test"))
        content_buffer = io.BytesIO()
        with wave.open(content_buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16_000)
            output.writeframes(b"\0\0" * 800)
        content = content_buffer.getvalue()
        hashes = [hashlib.sha256(content).hexdigest()]
        identity = file_identity_hash(len(content), hashes)
        session = manager.create("commit-test", "committed.wav", len(content), None, identity, hashes)
        manager.put_chunk(session["upload_id"], 0, content, hashes[0])

        real_mark_completed = manager.mark_completed

        def fail_bookkeeping(_upload_id: str, _recording_id: str) -> None:
            raise OSError("simulated disk full while updating upload session")

        with patch.object(main, "repository", repository), patch.object(main, "audio_uploads", manager), patch.object(
            manager, "mark_completed", side_effect=fail_bookkeeping
        ):
            with pytest.raises(OSError, match="simulated disk full"):
                main._finalize_source_recording_upload("commit-test", session["upload_id"])
            recording_id = repository.find_source_recording_for_upload("commit-test", session["upload_id"])
            assert recording_id is not None
            recording_path = repository.managed_source_recording_path(recording_id)
            assert recording_path.is_file()
            assert recording_path.stat().st_size > 44
            assert (manager.recovery_root / f"{session['upload_id']}.json").is_file()

            with patch.object(manager, "mark_completed", wraps=real_mark_completed):
                recovered = main._finalize_source_recording_upload("commit-test", session["upload_id"])
            assert recovered.id == recording_id
            assert recording_path.is_file()
            assert not (manager.recovery_root / f"{session['upload_id']}.json").exists()
    finally:
        repository.close()
