import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from speechcraft_dataset.model_manifest import ManifestError, parse_manifest, verify_artifact


def tiny_manifest():
    roles = ("diarization", "asr", "speech_detection", "speaker_purity")
    return {
        "schema_version": 1,
        "models": [
            {
                "id": role,
                "role": role,
                "name": role,
                "license": {"identifier": "fixture"},
                "access": {"requirements": []},
                "runtime_validation": {"status": "deferred"},
                "artifacts": [{
                    "platform": "test",
                    "format": "fixture",
                    "revision": "fixture-1",
                    "availability": "verified",
                    "required_files": [
                        {"path": "weights/a.bin", "size_bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()},
                        {"path": "config.json", "size_bytes": 2},
                    ],
                }],
            }
            for role in roles
        ],
    }


class ModelManifestTests(unittest.TestCase):
    def test_catalog_and_fixture_parse(self):
        root = Path(__file__).parents[1]
        parse_manifest(root / "model-catalog.json")
        parse_manifest(tiny_manifest())

    def test_requires_exact_four_roles(self):
        manifest = tiny_manifest()
        manifest["models"].pop()
        with self.assertRaises(ManifestError):
            parse_manifest(manifest)

    def test_rejects_path_traversal(self):
        manifest = tiny_manifest()
        manifest["models"][0]["artifacts"][0]["required_files"][0]["path"] = "../outside"
        with self.assertRaises(ManifestError):
            parse_manifest(manifest)

    def test_file_status_missing_partial_corrupt_verified(self):
        artifact = tiny_manifest()["models"][0]["artifacts"][0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(verify_artifact(root, artifact)["status"], "missing")
            (root / "weights").mkdir()
            (root / "weights/a.bin").write_bytes(b"a")
            self.assertEqual(verify_artifact(root, artifact)["status"], "partial")
            (root / "weights/a.bin").write_bytes(b"xyz")
            (root / "config.json").write_bytes(b"{}")
            self.assertEqual(verify_artifact(root, artifact)["status"], "corrupt")
            (root / "weights/a.bin").write_bytes(b"abc")
            self.assertEqual(verify_artifact(root, artifact)["status"], "verified")

    def test_unresolved_metadata_does_not_claim_verified_files(self):
        artifact = {"required_files": [{"path": "model.bin"}]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "model.bin").write_bytes(b"some bytes")
            self.assertEqual(verify_artifact(tmp, artifact)["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
