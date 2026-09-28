#!/usr/bin/env python3
"""Compile a Speechcraft canonical export into a VoxCPM train.jsonl.

Reads speechcraft_dataset.jsonl (snapshot-relative audio paths) and writes
the VoxCPM finetune manifest:

    {"audio": "/abs/path.wav", "text": "...", "duration": 3.67, "dataset_id": 0}

Does not resample. VoxCPM1.5 training configs resample to 44.1 kHz.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import wave
from pathlib import Path
from typing import Any


PROFILE_PATH = Path(__file__).resolve().parent / "profile.json"


class CompileError(ValueError):
    pass


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    if rate <= 0:
        raise CompileError(f"invalid sample rate in {path}")
    return round(frames / rate, 6)


def resolve_manifest_path(raw: Path) -> tuple[Path, Path]:
    path = raw.expanduser().resolve()
    if path.is_dir():
        manifest = path / "speechcraft_dataset.jsonl"
        if not manifest.is_file():
            raise CompileError(f"no speechcraft_dataset.jsonl in {path}")
        return path, manifest
    if path.name != "speechcraft_dataset.jsonl":
        raise CompileError(f"expected speechcraft_dataset.jsonl, got {path.name}")
    if not path.is_file():
        raise CompileError(f"manifest not found: {path}")
    return path.parent, path


def load_canonical_rows(manifest_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CompileError(f"{manifest_path}:{line_no}: invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CompileError(f"{manifest_path}:{line_no}: row is not an object")
        rows.append(payload)
    if not rows:
        raise CompileError(f"{manifest_path} contains no rows")
    return rows


def _require_str(payload: dict[str, Any], key: str, *, clip_id: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CompileError(f"{clip_id}: missing {key}")
    return value


def compile_row(
    row: dict[str, Any],
    *,
    snapshot_dir: Path,
    dataset_id: int,
    copy_dir: Path | None,
) -> dict[str, Any]:
    schema_version = row.get("schema_version")
    if schema_version != 1:
        raise CompileError(f"unsupported schema_version {schema_version!r}; expected 1")

    clip_id = _require_str(row, "clip_id", clip_id="<unknown>")
    transcript = row.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip():
        raise CompileError(f"{clip_id}: empty transcript")

    review = row.get("review")
    if isinstance(review, dict):
        status = review.get("status")
        if status != "accepted":
            raise CompileError(f"{clip_id}: review.status is {status!r}, expected accepted")

    audio = row.get("audio")
    if not isinstance(audio, dict):
        raise CompileError(f"{clip_id}: missing audio object")
    relative_path = _require_str(audio, "path", clip_id=clip_id)
    audio_path = (snapshot_dir / relative_path).resolve()
    if not audio_path.is_file():
        raise CompileError(f"{clip_id}: audio file missing: {audio_path}")

    expected_sha = audio.get("sha256")
    if isinstance(expected_sha, str) and expected_sha:
        actual_sha = sha256_file(audio_path)
        if actual_sha != expected_sha:
            raise CompileError(
                f"{clip_id}: audio sha256 mismatch (expected {expected_sha}, got {actual_sha})"
            )

    duration = audio.get("duration_sec")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = wav_duration_sec(audio_path)

    export_audio = audio_path
    if copy_dir is not None:
        dest = copy_dir / f"{clip_id}.wav"
        if dest.exists() and dest.resolve() != audio_path:
            raise CompileError(f"{clip_id}: refusing to overwrite {dest}")
        if dest.resolve() != audio_path:
            shutil.copy2(audio_path, dest)
        export_audio = dest

    record: dict[str, Any] = {
        "audio": str(export_audio),
        "text": transcript,
        "duration": round(float(duration), 3),
        "dataset_id": dataset_id,
    }
    return record


def compile_manifest(
    *,
    snapshot_dir: Path,
    rows: list[dict[str, Any]],
    dataset_id: int,
    copy_dir: Path | None,
) -> list[dict[str, Any]]:
    compiled: list[dict[str, Any]] = []
    for row in rows:
        compiled.append(
            compile_row(row, snapshot_dir=snapshot_dir, dataset_id=dataset_id, copy_dir=copy_dir)
        )
    return compiled


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile Speechcraft canonical export → VoxCPM JSONL")
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to speechcraft_dataset.jsonl or its parent snapshot directory",
    )
    parser.add_argument("--out", required=True, help="Output train.jsonl path")
    parser.add_argument(
        "--copy-audio",
        metavar="DIR",
        default="",
        help="Optional directory to copy WAVs into (still writes absolute paths)",
    )
    parser.add_argument(
        "--profile",
        default=str(PROFILE_PATH),
        help="profile.json path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        profile = load_profile(Path(args.profile))
        snapshot_dir, manifest_path = resolve_manifest_path(Path(args.manifest))
        copy_dir = Path(args.copy_audio).expanduser().resolve() if args.copy_audio else None
        if copy_dir is not None:
            copy_dir.mkdir(parents=True, exist_ok=True)
        rows = load_canonical_rows(manifest_path)
        compiled = compile_manifest(
            snapshot_dir=snapshot_dir,
            rows=rows,
            dataset_id=int(profile.get("dataset_id") or 0),
            copy_dir=copy_dir,
        )
        out_path = Path(args.out).expanduser().resolve()
        write_jsonl(out_path, compiled)
    except CompileError as exc:
        print(f"compile failed: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {len(compiled)} rows → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
