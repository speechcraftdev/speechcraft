"""Bounded, resumable audio uploads and safe media normalization."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

CHUNK_SIZE = 8 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024
MAX_WAV_BYTES = (2**32) - 4096
ALLOWED_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}
SESSION_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UploadError(ValueError):
    pass


class UploadIdentityMismatch(UploadError):
    pass


class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int
    sha256: str | None = None
    file_identity_sha256: str
    chunk_hashes: list[str] = Field(min_length=1)
    client_upload_id: str | None = None


def file_identity_hash(size: int, chunk_hashes: list[str]) -> str:
    digest_manifest = size.to_bytes(8, "big") + b"".join(bytes.fromhex(item) for item in chunk_hashes)
    return hashlib.sha256(digest_manifest).hexdigest()


class AudioUploadManager:
    def __init__(self, media_root: Path):
        self.media_root = media_root
        self.root = media_root / ".uploads"
        self.recovery_root = media_root / ".ingest-recovery"
        self.root.mkdir(parents=True, exist_ok=True)
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._finalize_locks: dict[str, threading.Lock] = {}
        self.cleanup_stale()

    def write_promotion_marker(self, upload_id: str, recording_id: str, filenames: list[str]) -> Path:
        if not SESSION_ID_RE.fullmatch(upload_id) or not re.fullmatch(r"source-[0-9a-f]{32}", recording_id):
            raise UploadError("Invalid promotion identity")
        if not filenames or any(Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) for name in filenames):
            raise UploadError("Invalid promotion file list")
        marker = self.recovery_root / f"{upload_id}.json"
        temp = marker.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump({"upload_id": upload_id, "recording_id": recording_id, "filenames": filenames}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, marker)
        return marker

    def remove_promotion_marker(self, upload_id: str) -> None:
        if SESSION_ID_RE.fullmatch(upload_id):
            (self.recovery_root / f"{upload_id}.json").unlink(missing_ok=True)

    @contextmanager
    def finalization_lock(self, upload_id: str):
        with self._lock:
            lock = self._finalize_locks.setdefault(upload_id, threading.Lock())
        with lock:
            yield

    def cleanup_stale(self, max_age_seconds: int = 24 * 60 * 60) -> None:
        now = __import__("time").time()
        for path in self.root.iterdir():
            try:
                if now - path.stat().st_mtime > max_age_seconds:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    def _session(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        if not SESSION_ID_RE.fullmatch(upload_id):
            raise UploadError("Invalid upload id")
        folder = self.root / upload_id
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.root.resolve()):
            raise UploadError("Invalid upload session path")
        try:
            data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise KeyError(upload_id) from exc
        return folder, data

    def create(
        self, project_id: str, filename: str, size: int, file_hash: str | None,
        file_identity_sha256: str, chunk_hashes: list[str], client_upload_id: str | None = None,
    ) -> dict[str, Any]:
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not name or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
            raise UploadError("Supported formats: WAV, MP3, FLAC, M4A, AAC, OGG, Opus, and AIFF")
        if size <= 0 or size > MAX_UPLOAD_BYTES:
            raise UploadError("File must be larger than zero and no larger than 8 GiB")
        if file_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", file_hash):
            raise UploadError("Invalid file SHA-256")
        expected_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        if len(chunk_hashes) != expected_chunks or any(not re.fullmatch(r"[0-9a-fA-F]{64}", item) for item in chunk_hashes):
            raise UploadError("Invalid chunk hash manifest")
        normalized_chunk_hashes = [item.lower() for item in chunk_hashes]
        actual_identity = file_identity_hash(size, normalized_chunk_hashes)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", file_identity_sha256) or actual_identity != file_identity_sha256.lower():
            raise UploadError("File identity did not match its chunk hashes")
        if client_upload_id is not None and not SESSION_ID_RE.fullmatch(client_upload_id):
            raise UploadError("Invalid client upload id")
        if client_upload_id:
            for existing_folder in self.root.iterdir():
                try:
                    existing = json.loads((existing_folder / "session.json").read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError):
                    continue
                if existing.get("client_upload_id") == client_upload_id:
                    if (existing.get("project_id"), existing.get("filename"), existing.get("size"), existing.get("file_identity_sha256")) != (
                        project_id, name, size, actual_identity
                    ):
                        raise UploadError("Client upload id was already used for a different file")
                    return self.status(existing["id"])
        peak_staging_bytes = 2 * size + 128 * 1024 * 1024
        if shutil.disk_usage(self.root).free < peak_staging_bytes:
            raise UploadError("Not enough free disk space to import this recording")
        upload_id = uuid.uuid4().hex
        folder = self.root / upload_id
        folder.mkdir(mode=0o700)
        data = {"id": upload_id, "project_id": project_id, "filename": name, "size": size,
                "file_hash": file_hash.lower() if file_hash else None, "chunk_size": CHUNK_SIZE,
                "file_identity_sha256": actual_identity, "chunk_hashes": normalized_chunk_hashes,
                "client_upload_id": client_upload_id, "chunks": {}, "created": __import__("time").time()}
        self._save(folder, data)
        return self.status(upload_id)

    @staticmethod
    def _save(folder: Path, data: dict[str, Any]) -> None:
        tmp = folder / "session.json.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, folder / "session.json")

    def status(
        self, upload_id: str, *, verify_chunks: bool = False, expected_identity: str | None = None
    ) -> dict[str, Any]:
        folder, data = self._session(upload_id)
        if expected_identity is not None and data.get("file_identity_sha256") != expected_identity.lower():
            raise UploadIdentityMismatch("Selected file does not match this upload session")
        if verify_chunks:
            with self._lock:
                changed = False
                for index in list(data["chunks"]):
                    chunk = folder / f"{int(index):08d}.chunk"
                    metadata = data["chunks"][index]
                    digest = hashlib.sha256()
                    byte_count = 0
                    try:
                        with chunk.open("rb") as stream:
                            while block := stream.read(1024 * 1024):
                                digest.update(block)
                                byte_count += len(block)
                    except OSError:
                        byte_count = -1
                    if byte_count != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
                        chunk.unlink(missing_ok=True)
                        del data["chunks"][index]
                        changed = True
                if changed:
                    self._save(folder, data)
        indexes = sorted(int(index) for index in data["chunks"])
        return {"upload_id": upload_id, "chunk_size": data["chunk_size"],
                "chunk_count": (data["size"] + data["chunk_size"] - 1) // data["chunk_size"],
                "completed_chunks": indexes, "committed_bytes": sum(data["chunks"][str(i)]["size"] for i in indexes),
                "file_identity_sha256": data["file_identity_sha256"]}

    def put_chunk(self, upload_id: str, index: int, content: bytes, digest: str) -> dict[str, Any]:
        with self._lock:
            folder, data = self._session(upload_id)
            if index < 0 or index >= (data["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE:
                raise UploadError("Invalid chunk index")
            expected = min(CHUNK_SIZE, data["size"] - index * CHUNK_SIZE)
            actual_hash = hashlib.sha256(content).hexdigest()
            expected_hash = data["chunk_hashes"][index]
            if (len(content) != expected or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                    or actual_hash != digest.lower() or actual_hash != expected_hash):
                raise UploadError("Chunk size or SHA-256 did not match")
            chunk = folder / f"{index:08d}.chunk"
            previous = data["chunks"].get(str(index))
            if previous:
                if previous["sha256"] != actual_hash:
                    raise UploadError("A different verified chunk already exists at this index")
            else:
                temp = chunk.with_suffix(".tmp")
                with temp.open("wb") as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, chunk)
                data["chunks"][str(index)] = {"size": len(content), "sha256": actual_hash}
                self._save(folder, data)
            return self.status(upload_id)

    def assemble(self, upload_id: str, destination: Path) -> tuple[dict[str, Any], str]:
        with self._lock:
            folder, data = self._session(upload_id)
            if data.get("completed"):
                return data, data["assembled_sha256"]
            count = (data["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
            if file_identity_hash(data["size"], data["chunk_hashes"]) != data["file_identity_sha256"]:
                raise UploadError("Upload session file identity failed integrity verification")
            if len(data["chunks"]) != count:
                raise UploadError("Upload is incomplete")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix(destination.suffix + ".part")
            digest = hashlib.sha256()
            with temp.open("wb") as out:
                for index in range(count):
                    chunk = folder / f"{index:08d}.chunk"
                    chunk_digest = hashlib.sha256()
                    chunk_size = 0
                    with chunk.open("rb") as source:
                        while block := source.read(1024 * 1024):
                            digest.update(block)
                            chunk_digest.update(block)
                            out.write(block)
                            chunk_size += len(block)
                    metadata = data["chunks"][str(index)]
                    if chunk_size != metadata["size"] or chunk_digest.hexdigest() != metadata["sha256"]:
                        temp.unlink(missing_ok=True)
                        raise UploadError("A completed upload chunk failed integrity verification")
                out.flush()
                os.fsync(out.fileno())
            actual = digest.hexdigest()
            if temp.stat().st_size != data["size"] or (data["file_hash"] and actual != data["file_hash"]):
                temp.unlink(missing_ok=True)
                raise UploadError("Completed file failed size or SHA-256 verification")
            os.replace(temp, destination)
            data["assembled_sha256"] = actual
            self._save(folder, data)
            return data, actual

    def mark_completed(self, upload_id: str, recording_id: str) -> None:
        folder, data = self._session(upload_id)
        data["completed"] = True
        data["recording_id"] = recording_id
        self._save(folder, data)

    def completed_recording(self, upload_id: str) -> str | None:
        _, data = self._session(upload_id)
        return data.get("recording_id")

    def remove(self, upload_id: str) -> None:
        if SESSION_ID_RE.fullmatch(upload_id):
            shutil.rmtree(self.root / upload_id, ignore_errors=True)
