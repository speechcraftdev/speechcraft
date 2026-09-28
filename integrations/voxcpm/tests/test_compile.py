from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path

_COMPILE_PATH = Path(__file__).resolve().parents[1] / "compile.py"
_SPEC = importlib.util.spec_from_file_location("voxcpm_compile", _COMPILE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
compile_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compile_mod)

CompileError = compile_mod.CompileError
compile_manifest = compile_mod.compile_manifest
load_canonical_rows = compile_mod.load_canonical_rows
resolve_manifest_path = compile_mod.resolve_manifest_path
write_jsonl = compile_mod.write_jsonl


def write_wav(path: Path, *, frames: int = 1600, sample_rate: int = 16000) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def canonical_row(clip_id: str, audio_rel: str, sha256: str, transcript: str, duration: float) -> dict:
    return {
        "schema_version": 1,
        "clip_id": clip_id,
        "transcript": transcript,
        "lineage": {"source_clip_id": clip_id, "parent_clip_ids": []},
        "audio": {
            "path": audio_rel,
            "kind": "candidate_original",
            "sha256": sha256,
            "source_audio_sha256": sha256,
            "sample_rate_hz": 16000,
            "channels": 1,
            "duration_sec": duration,
        },
        "review": {"status": "accepted", "reviewer_tags": []},
    }


class CompileTests(unittest.TestCase):
    def test_compile_resolves_snapshot_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            run_root = root / "run"
            clips = run_root / "artifacts" / "candidate_review_clips"
            snapshot = run_root / "artifacts" / "canonical_exports" / "canonical_export_test"
            wav = clips / "candidate_review_clip_000001.wav"
            sha = write_wav(wav, frames=8000)
            row = canonical_row(
                "candidate_review_clip_000001",
                "../../candidate_review_clips/candidate_review_clip_000001.wav",
                sha,
                "Hello from Clip Lab.",
                0.5,
            )
            snapshot.mkdir(parents=True)
            manifest = snapshot / "speechcraft_dataset.jsonl"
            write_jsonl(manifest, [row])

            snapshot_dir, manifest_path = resolve_manifest_path(snapshot)
            compiled = compile_manifest(
                snapshot_dir=snapshot_dir,
                rows=load_canonical_rows(manifest_path),
                dataset_id=0,
                copy_dir=None,
            )
            self.assertEqual(len(compiled), 1)
            self.assertEqual(compiled[0]["text"], "Hello from Clip Lab.")
            self.assertEqual(compiled[0]["audio"], str(wav.resolve()))
            self.assertEqual(compiled[0]["duration"], 0.5)
            self.assertEqual(compiled[0]["dataset_id"], 0)

    def test_compile_rejects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wav = root / "clip.wav"
            write_wav(wav)
            row = canonical_row("clip_1", "clip.wav", "ab" * 32, "ok", 0.1)
            with self.assertRaises(CompileError):
                compile_manifest(
                    snapshot_dir=root,
                    rows=[row],
                    dataset_id=0,
                    copy_dir=None,
                )


if __name__ == "__main__":
    unittest.main()
