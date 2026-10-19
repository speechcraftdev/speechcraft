import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock


INSTALLER = pathlib.Path(__file__).resolve().parents[1] / "install-linux.py"
SPEC = importlib.util.spec_from_file_location("speechcraft_install_linux", INSTALLER)
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


def sha(value):
    return hashlib.sha256(value).hexdigest()


class InstallerTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="speechcraft-installer-")
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "user home"
        self.home.mkdir()
        self.old_home = os.environ.get("HOME")
        self.home_patch = mock.patch.object(pathlib.Path, "home", return_value=self.home)
        self.home_patch.start()

    def tearDown(self):
        self.home_patch.stop()
        self.temp.cleanup()

    def make_archive(self, version="0.1.0", omit=None):
        runtime_manifest = {
            "worker": {"validation_helper": "workers/dataset/scripts/preflight.py"},
            "runtime_validation": {
                "probes": [{"helper": "workers/dataset/scripts/runtime_probe.py"}],
                "fixtures": {"speech": {"path": "workers/dataset/runtime-fixtures/speech.wav"}},
            },
        }
        package = {
            "runtime/node": (b"#!/bin/sh\nprintf 'v24.22.0\\n'\n", 0o755),
            "bin/speechcraft": (b"#!/bin/sh\necho speechcraft-fixture\n", 0o755),
            "setup-controller/resources/runtime-manifest.json": (json.dumps(runtime_manifest).encode(), 0o644),
        }
        for relative in INSTALL.MANDATORY_RUNTIME_FILES:
            if relative != "setup-controller/resources/runtime-manifest.json":
                package[relative] = (b"fixture-resource", 0o644)
        if omit:
            package.pop(omit, None)
        records = [{"path": name, "size_bytes": len(content), "sha256": sha(content), "executable": bool(mode & 0o111)}
                   for name, (content, mode) in package.items()]
        internal = {"schema_version": 1, "application": "SpeechCraft", "version": version,
                    "platform": "linux-x86_64", "node": {"version": "24.22.0", "executable": "runtime/node",
                    "sha256": sha(package["runtime/node"][0])}, "runtime_inventory": {
                    "status": "complete", "required_files": sorted(INSTALL.MANDATORY_RUNTIME_FILES)},
                    "package_files": records}
        internal_bytes = (json.dumps(internal, sort_keys=True) + "\n").encode()
        app_name = f"speechcraft-{version}-linux-x86_64"
        members = {**{f"{app_name}/{name}": value for name, value in package.items()},
                   f"{app_name}/application-release-manifest.json": (internal_bytes, 0o644)}
        archive = self.root / f"{app_name}.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for name, (content, mode) in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = mode
                output.addfile(info, io.BytesIO(content))
        archive_bytes = archive.read_bytes()
        digest = sha(archive_bytes)
        archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
        external = {"schema_version": 1, "archive": archive.name, "archive_size_bytes": len(archive_bytes),
                    "archive_sha256": digest, "platform": "linux-x86_64", "application_version": version,
                    "node_version": "24.22.0", "application_manifest_sha256": sha(internal_bytes)}
        archive.with_name(archive.name + ".release.json").write_text(json.dumps(external), encoding="utf8")
        return archive

    def install(self, archive):
        INSTALL.main(str(archive))

    def current(self):
        return self.home / ".local/share/speechcraft/app/current"

    def test_clean_install_idempotency_and_upgrade(self):
        first = self.make_archive("0.1.0")
        self.install(first)
        self.assertEqual(self.current().resolve().name, "speechcraft-0.1.0-linux-x86_64")
        command = self.home / ".local/bin/speechcraft"
        self.assertIn(b"SpeechCraft per-user command wrapper", command.read_bytes())
        previous = os.readlink(self.current())
        self.install(first)
        self.assertEqual(os.readlink(self.current()), previous)
        second = self.make_archive("0.2.0")
        self.install(second)
        self.assertEqual(self.current().resolve().name, "speechcraft-0.2.0-linux-x86_64")
        self.assertTrue((self.home / ".local/share/speechcraft/app/releases/speechcraft-0.1.0-linux-x86_64").is_dir())

    def test_unrelated_command_collision_does_not_change_current_pointer(self):
        archive = self.make_archive()
        self.install(archive)
        before = os.readlink(self.current())
        command = self.home / ".local/bin/speechcraft"
        command.write_text("#!/bin/sh\necho unrelated\n", encoding="utf8")
        with self.assertRaises(SystemExit):
            self.install(archive)
        self.assertEqual(os.readlink(self.current()), before)
        self.assertIn("unrelated", command.read_text(encoding="utf8"))

    def test_pointer_activation_failure_restores_command_and_previous_release(self):
        first = self.make_archive("0.1.0")
        self.install(first)
        prior_pointer = os.readlink(self.current())
        command = self.home / ".local/bin/speechcraft"
        prior_command = command.read_bytes()
        second = self.make_archive("0.2.0")
        real_replace = os.replace

        def fail_current(source, target):
            if pathlib.Path(target) == self.current() and pathlib.Path(source).name.startswith(".current-"):
                raise OSError("injected activation interruption")
            return real_replace(source, target)

        with mock.patch.object(INSTALL.os, "replace", side_effect=fail_current):
            with self.assertRaises(SystemExit):
                self.install(second)
        self.assertEqual(os.readlink(self.current()), prior_pointer)
        self.assertEqual(command.read_bytes(), prior_command)
        self.assertTrue((self.home / ".local/share/speechcraft/app/releases/speechcraft-0.2.0-linux-x86_64").is_dir(),
                        "verified staged release can remain inactive without replacing the last known-good release")

    def test_command_registration_failure_keeps_previous_current(self):
        first = self.make_archive("0.1.0")
        self.install(first)
        pointer = os.readlink(self.current())
        second = self.make_archive("0.2.0")
        real_replace = os.replace

        def fail_command(source, target):
            if pathlib.Path(target) == self.home / ".local/bin/speechcraft":
                raise OSError("injected command registration failure")
            return real_replace(source, target)

        with mock.patch.object(INSTALL.os, "replace", side_effect=fail_command):
            with self.assertRaises(SystemExit):
                self.install(second)
        self.assertEqual(os.readlink(self.current()), pointer)

    def test_corrupt_or_incomplete_archive_is_rejected_before_activation(self):
        first = self.make_archive("0.1.0")
        self.install(first)
        pointer = os.readlink(self.current())
        missing = self.make_archive("0.2.0", omit="workers/dataset/scripts/preflight.py")
        with self.assertRaises(SystemExit):
            self.install(missing)
        self.assertEqual(os.readlink(self.current()), pointer)
        with open(missing, "ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(SystemExit):
            self.install(missing)
        self.assertEqual(os.readlink(self.current()), pointer)


    def test_progress_checkpoints_are_exact_counts(self):
        self.assertEqual(INSTALL.progress_checkpoints(0), ())
        self.assertEqual(INSTALL.progress_checkpoints(1), (1,))
        self.assertEqual(INSTALL.progress_checkpoints(4), (2, 4))

    def test_extraction_and_verification_milestones_are_not_per_file(self):
        archive = self.make_archive("0.1.0")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.install(archive)
        text = buffer.getvalue()
        extracted = re.findall(r"extracted (\d+) of (\d+) files", text)
        verified = re.findall(r"verified (\d+) of (\d+) packaged files", text)
        self.assertEqual(extracted[-1][0], extracted[-1][1])
        self.assertEqual(verified[-1][0], verified[-1][1])
        total = int(extracted[-1][1])
        self.assertGreater(total, 2)
        self.assertEqual(len(extracted), 2)
        self.assertEqual(int(extracted[0][0]), total // 2)
        self.assertEqual(len(verified), 2)
        self.assertNotIn("%", text)
        self.assertIn("archive SHA-256 verified", text)
        self.assertIn("activating the launcher command", text)
        self.assertIn("Installed SpeechCraft 0.1.0", text)
        self.assertIn("Persistent data remains", text)
        for line in text.splitlines():
            if line.startswith("SpeechCraft installer:"):
                self.assertNotIn("backend/app/main.py", line)
        self.assertLess(text.index("extracted "), text.index("verifying "))
        self.assertLess(text.index("verifying "), text.index("verified "))
        self.assertLess(text.index("verified "), text.index("activating the launcher command"))

    def test_failed_archive_check_uses_stderr_and_does_not_claim_success(self):
        archive = self.make_archive("0.1.0")
        archive.write_bytes(archive.read_bytes() + b"tamper")
        result = subprocess.run([sys.executable, str(INSTALLER), str(archive)],
                                env={**os.environ, "HOME": str(self.home)},
                                text=True, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive SHA-256 verification failed", result.stderr)
        self.assertNotIn("verified", result.stdout.lower())
        self.assertNotIn("Installed", result.stdout)
        self.assertFalse(self.current().exists())


if __name__ == "__main__":
    unittest.main()
