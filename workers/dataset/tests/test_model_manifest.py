import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from speechcraft_dataset.model_manifest import (
    ManifestError, SCHEMA_PATH, parse_manifest, select_artifact, validate_manifest,
    verify_artifact,
)


def tiny_manifest():
    """All identities and hashes here refer only to fabricated test data."""
    return {
        "schema_version": 1,
        "models": [
            {
                "id": role, "role": role, "name": role,
                "license": {"identifier": "fixture", "source": "fixture"},
                "access": {"requirements": []},
                "runtime_validation": {"status": "deferred", "contract": ["future fixture check"]},
                "artifacts": [{
                    "platform": "linux-x86_64", "format": "fixture",
                    "revision": "a" * 40, "availability": "verified", "inventory_complete": True,
                    "required_files": [
                        {"path": "weights/a.bin", "size_bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()},
                        {"path": "config.json", "size_bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()},
                    ],
                }],
            }
            for role in ("diarization", "asr", "speech_detection", "speaker_purity")
        ],
    }


class ModelManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "model"
        self.root.mkdir()
        self.manifest = tiny_manifest()
        self.artifact = self.manifest["models"][0]["artifacts"][0]

    def write_files(self):
        (self.root / "weights").mkdir(exist_ok=True)
        (self.root / "weights/a.bin").write_bytes(b"abc")
        (self.root / "config.json").write_bytes(b"{}")

    def check(self):
        return verify_artifact(self.root, self.artifact)

    def test_catalog_and_fixture_parse(self):
        catalog = parse_manifest(SCHEMA_PATH.with_name("model-catalog.json"))
        parse_manifest(self.manifest)
        for model in catalog["models"]:
            for artifact in model["artifacts"]:
                result = verify_artifact(self.root, artifact)
                self.assertFalse(result["install_ready"])

    def test_each_schema_required_field_is_enforced(self):
        schema = json.loads(SCHEMA_PATH.read_text())

        def walk(value, rule, location):
            if "$ref" in rule:
                rule = schema["$defs"][rule["$ref"].split("/")[-1]]
            if isinstance(value, dict):
                for field in rule.get("required", []):
                    saved = value.pop(field)
                    try:
                        with self.subTest(location=location, field=field):
                            with self.assertRaises(ManifestError):
                                validate_manifest(self.manifest)
                    finally:
                        value[field] = saved
                for field, item in value.items():
                    if field in rule.get("properties", {}):
                        walk(item, rule["properties"][field], location + "." + field)
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, rule["items"], f"{location}[{index}]")

        walk(self.manifest, schema, "manifest")

    def test_malformed_types_and_unknown_fields_are_manifest_errors(self):
        cases = [
            (self.manifest, "schema_version", True),
            (self.manifest, "extra", "value"),
            (self.manifest["models"][0], "license", "MIT"),
            (self.artifact, "required_files", None),
            (self.artifact, "platform", []),
            (self.artifact, "availability", "ready"),
            (self.artifact, "inventory_complete", "yes"),
            (self.artifact["required_files"][0], "sha256", 123),
            (self.artifact["required_files"][0], "sha256", "a" * 64 + "\n"),
            (self.artifact["required_files"][0], "size_bytes", True),
            (self.artifact["required_files"][0], "size_bytes", -1),
            (self.artifact["required_files"][0], "min_size_bytes", 4),
        ]
        for obj, key, value in cases:
            with self.subTest(key=key, value=value):
                saved = dict(obj)
                obj[key] = value
                try:
                    with self.assertRaises(ManifestError):
                        parse_manifest(self.manifest)
                finally:
                    obj.clear()
                    obj.update(saved)

    def test_duplicate_roles_platforms_and_files(self):
        for target in ("roles", "platforms", "files"):
            with self.subTest(target=target):
                manifest = tiny_manifest()
                if target == "roles":
                    manifest["models"][-1] = manifest["models"][0]
                else:
                    artifacts = manifest["models"][0]["artifacts"]
                    items = artifacts if target == "platforms" else artifacts[0]["required_files"]
                    items.append(copy.deepcopy(items[0]))
                with self.assertRaises(ManifestError):
                    parse_manifest(manifest)

    def test_unsafe_paths_are_rejected_by_direct_verifier(self):
        for path in ("../outside", "/absolute", ".", "a/../b", "./a", "a//b", "a/", "C:/file", "a\\b", "a\x00b"):
            with self.subTest(path=path):
                self.artifact["required_files"][0]["path"] = path
                with self.assertRaises(ManifestError):
                    self.check()

    def test_missing_partial_corrupt_verified(self):
        self.assertEqual(self.check()["status"], "missing")
        self.write_files()
        (self.root / "weights/a.bin").write_bytes(b"a")
        self.assertEqual(self.check()["status"], "partial")
        (self.root / "weights/a.bin").write_bytes(b"xyz")
        result = self.check()
        self.assertEqual(result["status"], "corrupt")
        self.assertTrue(result["present"])
        self.assertTrue(result["complete"])
        self.assertFalse(result["integrity_verified"])
        (self.root / "weights/a.bin").write_bytes(b"abc")
        result = self.check()
        self.assertEqual(result["status"], "verified")
        self.assertTrue(result["install_ready"])
        self.assertIsNone(result["runtime_ready"])
        (self.root / "config.json").unlink()
        self.assertEqual(self.check()["status"], "partial")

    def test_size_only_cannot_verify_equal_sized_corruption(self):
        self.write_files()
        del self.artifact["required_files"][0]["sha256"]
        (self.root / "weights/a.bin").write_bytes(b"xyz")
        result = self.check()
        self.assertEqual(result["status"], "unverified")
        self.assertTrue(result["complete"])
        self.assertFalse(result["integrity_verified"])
        self.assertFalse(result["install_ready"])

    def test_empty_unresolved_inventory_never_verifies(self):
        self.artifact.update(required_files=[], inventory_complete=False, availability="unresolved")
        result = self.check()
        self.assertEqual(result["status"], "unverified")
        for field in ("present", "complete", "integrity_verified", "install_ready"):
            self.assertFalse(result[field])
        self.artifact["inventory_complete"] = True
        with self.assertRaises(ManifestError):
            self.check()

    def test_incomplete_inventory_and_release_blockers_prevent_install(self):
        self.write_files()
        self.artifact["inventory_complete"] = False
        result = self.check()
        self.assertTrue(result["present"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["install_ready"])
        self.artifact["inventory_complete"] = True
        self.artifact["availability"] = "unresolved"
        result = self.check()
        self.assertTrue(result["integrity_verified"])
        self.assertFalse(result["install_ready"])
        self.artifact["availability"] = "verified"
        self.artifact["release_blockers"] = ["licensing unresolved"]
        self.assertFalse(self.check()["install_ready"])

    def test_minimum_size_is_not_integrity(self):
        self.write_files()
        self.artifact["required_files"][0] = {"path": "weights/a.bin", "min_size_bytes": 3}
        self.assertEqual(self.check()["status"], "unverified")
        (self.root / "weights/a.bin").write_bytes(b"a")
        self.assertEqual(self.check()["status"], "partial")

    def test_oversized_file_is_corrupt(self):
        self.write_files()
        (self.root / "config.json").write_bytes(b"too long")
        self.assertEqual(self.check()["status"], "corrupt")

    def test_symlink_leaf_parent_and_root_are_rejected(self):
        self.write_files()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "a.bin").write_bytes(b"abc")
        leaf = self.root / "weights/a.bin"
        leaf.unlink()
        leaf.symlink_to(outside / "a.bin")
        self.assertEqual(self.check()["status"], "error")
        leaf.unlink()
        (self.root / "weights").rmdir()
        (self.root / "weights").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.check()["status"], "error")
        link = Path(self.temp.name) / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(verify_artifact(link, self.artifact)["status"], "error")

    def test_unreadable_files_return_error(self):
        self.write_files()
        with patch("speechcraft_dataset.model_manifest.os.open", side_effect=PermissionError("denied")):
            result = self.check()
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["install_ready"])

    def test_platform_exact_over_shared_without_unresolved_fallback(self):
        artifacts = self.manifest["models"][0]["artifacts"]
        shared = copy.deepcopy(self.artifact)
        shared.update(platform="all-supported", supported_platforms=["linux-x86_64", "macos-arm64"])
        artifacts.insert(0, shared)
        self.assertIs(select_artifact(self.manifest, "diarization", "linux-x86_64"), self.artifact)
        self.assertIs(select_artifact(self.manifest, "diarization", "macos-arm64"), shared)
        mac = copy.deepcopy(self.artifact)
        mac.update(platform="macos-arm64", availability="unresolved", required_files=[], inventory_complete=False)
        artifacts.append(mac)
        self.assertIs(select_artifact(self.manifest, "diarization", "macos-arm64"), mac)
        with self.assertRaises(ManifestError):
            select_artifact(self.manifest, "diarization", "windows-arm64")
        with self.assertRaises(ManifestError):
            select_artifact(self.manifest, "asr", "macos-arm64")

    def test_shared_artifact_needs_explicit_platforms(self):
        self.artifact["platform"] = "all-supported"
        with self.assertRaises(ManifestError):
            parse_manifest(self.manifest)

    def test_cli_exit_codes_and_json(self):
        path = Path(self.temp.name) / "manifest.json"

        def run():
            path.write_text(json.dumps(self.manifest))
            result = subprocess.run(
                [sys.executable, "-m", "speechcraft_dataset.model_manifest", str(path),
                 "diarization", "linux-x86_64", str(self.root)],
                cwd=SCHEMA_PATH.parent, capture_output=True, text=True, check=False,
            )
            return result.returncode, json.loads(result.stdout)["status"]

        self.assertEqual(run(), (1, "missing"))
        self.write_files()
        self.assertEqual(run(), (0, "verified"))
        (self.root / "config.json").write_bytes(b"xx")
        self.assertEqual(run(), (1, "corrupt"))
        (self.root / "config.json").write_bytes(b"{}")
        self.artifact["availability"] = "unresolved"
        self.assertEqual(run(), (1, "unverified"))
        del self.artifact["required_files"]
        self.assertEqual(run(), (2, "error"))


if __name__ == "__main__":
    unittest.main()
