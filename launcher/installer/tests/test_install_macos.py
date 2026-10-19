import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import tarfile
import io
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "launcher/installer/install-macos.sh"


class MacInstallerSimulationTests(unittest.TestCase):
    """Run only shell-level installer logic with a disposable fake Mac home."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="speechcraft-mac-install-test-")
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.mockbin = self.root / "mockbin"
        self.mockbin.mkdir()
        self._mock("uname", "#!/bin/sh\nif [ \"$1\" = -s ]; then echo Darwin; else echo \"${UNAME_MACHINE:-arm64}\"; fi\n")
        self._mock("stat", "#!/bin/sh\n[ \"$1\" = -f ] && [ \"$2\" = '%z' ] || exit 2\nwc -c < \"$3\" | tr -d '[:space:]'\n")
        self._mock("plutil", "#!/bin/sh\n[ \"$1\" = -extract ] && [ \"$3\" = raw ] && [ \"$4\" = -o ] && [ \"$5\" = - ] || exit 2\npython3 - \"$2\" \"$6\" <<'PY'\nimport json,sys\nx=json.load(open(sys.argv[2]));\nfor k in sys.argv[1].split('.'): x=x[k]\nprint(str(x).lower() if isinstance(x,bool) else x)\nPY\n")
        system_mv = shutil.which("mv")
        self._mock("mv", f"#!/bin/sh\nif [ \"$1\" = -fh ]; then [ \"${{MOCK_FAIL_ACTIVATION:-}}\" = 1 ] && exit 1; shift; exec {system_mv} -T -f \"$@\"; fi\nexec {system_mv} \"$@\"\n")
        system_df = shutil.which("df")
        self._mock("df", f"#!/bin/sh\nif [ -n \"${{MOCK_FREE_KB:-}}\" ]; then printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\nfixture 10 9 %s 90%% /\\n' \"$MOCK_FREE_KB\"; else exec {system_df} \"$@\"; fi\n")
        system_tar = shutil.which("tar")
        self._mock("tar", f"#!/bin/sh\ncase \"$*\" in *-xzf*) [ \"${{MOCK_FAIL_EXTRACT:-}}\" = 1 ] && exit 2 ;; esac\nexec {system_tar} \"$@\"\n")

    def _mock(self, name, content):
        file = self.mockbin / name
        file.write_text(content)
        file.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, version="0.1.1", unsafe=None, symlink=False, unsafe_type=None):
        app = self.assets / f"speechcraft-{version}-macos-arm64"
        (app / "bin").mkdir(parents=True)
        (app / "runtime").mkdir()
        (app / "bin/speechcraft").write_text("#!/bin/sh\nexit 0\n")
        (app / "bin/speechcraft").chmod(0o755)
        (app / "runtime/node").write_text("fixture Mach-O placeholder\n")
        (app / "runtime/node").chmod(0o755)
        for member in ("frontend/(setup)/page.js", "frontend/node_modules/@next/swc-darwin-arm64/binding.node",
                       "frontend/[account]/page.js", "frontend/a name/app.js"):
            target = app / member
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("safe filename\n")
        manifest = {"schema_version": 1, "application": "SpeechCraft", "version": version,
                    "platform": "macos-arm64", "source_revision": "a" * 40,
                    "signing": {"status": "unsigned_development_build", "notarized": False}}
        (app / "application-release-manifest.json").write_text(json.dumps(manifest) + "\n")
        archive = self.assets / f"speechcraft-{version}-macos-arm64.tar.gz"
        if unsafe:
            (app / unsafe).parent.mkdir(parents=True, exist_ok=True)
            (app / unsafe).write_text("unsafe")
        if symlink:
            (app / "escape").symlink_to("/tmp")
        if unsafe or unsafe_type:
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(app, arcname=app.name)
                if unsafe:
                    info = tarfile.TarInfo(unsafe)
                    data = b"unsafe"
                    info.size = len(data)
                    bundle.addfile(info, io.BytesIO(data))
                if unsafe_type == "hardlink":
                    info = tarfile.TarInfo(app.name + "/hardlink")
                    info.type = tarfile.LNKTYPE
                    info.linkname = app.name + "/bin/speechcraft"
                    bundle.addfile(info)
                elif unsafe_type == "fifo":
                    info = tarfile.TarInfo(app.name + "/pipe")
                    info.type = tarfile.FIFOTYPE
                    bundle.addfile(info)
        else:
            subprocess.run(["tar", "-czf", str(archive), "-C", str(self.assets), app.name], check=True)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return archive, digest, str(archive.stat().st_size)

    def run_install(self, archive, digest, size, **extra):
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{self.mockbin}:{os.environ['PATH']}", **extra}
        return subprocess.run(["sh", str(INSTALLER), str(archive), digest, size, "a" * 40],
                              env=env, text=True, capture_output=True, timeout=30)

    def test_fresh_repeat_and_upgrade_preserve_data_and_space_in_paths(self):
        archive, digest, size = self.archive("0.1.1")
        data = self.home / "Library/Application Support/SpeechCraft/setup-controller/models/user-model"
        data.parent.mkdir(parents=True)
        data.write_text("keep")
        first = self.run_install(archive, digest, size)
        self.assertEqual(first.returncode, 0, first.stderr)
        base = self.home / "Library/Application Support/SpeechCraft"
        self.assertTrue((base / "app/current/bin/speechcraft").exists())
        self.assertTrue((base / "bin/speechcraft").is_file())
        self.assertEqual(data.read_text(), "keep")
        profile = self.home / ".zprofile"
        contents = profile.read_text()
        repeated = self.run_install(archive, digest, size)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(profile.read_text(), contents)
        self.assertEqual(profile.read_text().count("SpeechCraft user command PATH"), 2)
        self.assertEqual(len(list((base / "app/releases").glob("speechcraft-*"))), 1)
        newer, newer_digest, newer_size = self.archive("0.2.0")
        upgraded = self.run_install(newer, newer_digest, newer_size)
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        self.assertEqual(os.readlink(base / "app/current"), "releases/speechcraft-0.2.0-macos-arm64")
        self.assertTrue((base / "app/releases/speechcraft-0.1.1-macos-arm64").is_dir())
        self.assertEqual(data.read_text(), "keep")

    def test_bad_digest_unsafe_path_and_archive_links_fail_before_activation(self):
        archive, digest, size = self.archive("0.1.1")
        corrupt = self.run_install(archive, "0" * 64, size)
        self.assertNotEqual(corrupt.returncode, 0)
        self.assertIn("SHA-256", corrupt.stderr)
        base = self.home / "Library/Application Support/SpeechCraft"
        self.assertFalse((base / "app/current").exists())
        absolute, absolute_digest, absolute_size = self.archive("0.4.0", unsafe="/tmp/speechcraft-escape")
        result = self.run_install(absolute, absolute_digest, absolute_size)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe archive path", result.stderr)
        for version, entry_type in (("0.5.0", "hardlink"), ("0.6.0", "fifo")):
            unsafe_entry, unsafe_digest, unsafe_size = self.archive(version, unsafe_type=entry_type)
            result = self.run_install(unsafe_entry, unsafe_digest, unsafe_size)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink, hard link, or special file", result.stderr)
        unsafe, unsafe_digest, unsafe_size = self.archive("0.2.0", unsafe="../escape")
        result = self.run_install(unsafe, unsafe_digest, unsafe_size)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe archive path", result.stderr)
        self.assertFalse((base / "app/current").exists())
        linked, linked_digest, linked_size = self.archive("0.3.0", symlink=True)
        result = self.run_install(linked, linked_digest, linked_size)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink, hard link, or special file", result.stderr)
        self.assertFalse((base / "app/current").exists())

    def test_unrelated_command_conflict_and_symlinked_application_root_are_refused(self):
        archive, digest, size = self.archive()
        base = self.home / "Library/Application Support/SpeechCraft"
        (base / "bin").mkdir(parents=True)
        command = base / "bin/speechcraft"
        command.write_text("#!/bin/sh\necho unrelated\n")
        result = self.run_install(archive, digest, size)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not an owned SpeechCraft wrapper", result.stderr)
        self.assertEqual(command.read_text(), "#!/bin/sh\necho unrelated\n")
        other_home = self.root / "other-home"
        other_home.mkdir()
        real_base = other_home / "real-app"
        real_base.mkdir()
        (other_home / "Library").mkdir()
        (other_home / "Library/Application Support").mkdir()
        (other_home / "Library/Application Support/SpeechCraft").symlink_to(real_base)
        env = {**os.environ, "HOME": str(other_home), "PATH": f"{self.mockbin}:{os.environ['PATH']}"}
        result = subprocess.run(["sh", str(INSTALLER), str(archive), digest, size, "a" * 40],
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe application directory", result.stderr)

    def test_wrong_architecture_is_rejected(self):
        archive, digest, size = self.archive()
        result = self.run_install(archive, digest, size, UNAME_MACHINE="x86_64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Apple Silicon", result.stderr)
        base = self.home / "Library/Application Support/SpeechCraft"
        self.assertFalse(base.exists())

    def test_failed_activation_rolls_back_and_failed_extraction_cleans_staging(self):
        first, first_digest, first_size = self.archive("0.1.1")
        installed = self.run_install(first, first_digest, first_size)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        base = self.home / "Library/Application Support/SpeechCraft"
        second, second_digest, second_size = self.archive("0.2.0")
        failed = self.run_install(second, second_digest, second_size, MOCK_FAIL_ACTIVATION="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("could not activate release pointer", failed.stderr)
        self.assertEqual(os.readlink(base / "app/current"), "releases/speechcraft-0.1.1-macos-arm64")
        self.assertFalse(list((base / "app/releases").glob(".staging.*")))
        third, third_digest, third_size = self.archive("0.3.0")
        failed = self.run_install(third, third_digest, third_size, MOCK_FAIL_EXTRACT="1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(os.readlink(base / "app/current"), "releases/speechcraft-0.1.1-macos-arm64")
        self.assertFalse(list((base / "app/releases").glob(".staging.*")))

    def test_insufficient_space_and_same_version_destination_conflict_are_safe(self):
        archive, digest, size = self.archive("0.1.1")
        insufficient = self.run_install(archive, digest, size, MOCK_FREE_KB="0")
        self.assertNotEqual(insufficient.returncode, 0)
        self.assertIn("insufficient free disk space", insufficient.stderr)
        base = self.home / "Library/Application Support/SpeechCraft"
        self.assertFalse((base / "app/current").exists())
        installed = self.run_install(archive, digest, size)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        pointer = os.readlink(base / "app/current")
        release = base / "app/releases/speechcraft-0.1.1-macos-arm64"
        (release / "modified-by-user").write_text("preserve conflict")
        conflict = self.run_install(archive, digest, size)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("different contents", conflict.stderr)
        self.assertEqual(os.readlink(base / "app/current"), pointer)
        self.assertEqual((release / "modified-by-user").read_text(), "preserve conflict")

    def test_existing_zprofile_is_preserved_and_symlink_is_refused_before_activation(self):
        profile = self.home / ".zprofile"
        profile.write_text("# user configuration\nexport KEEP_ME=1\n")
        archive, digest, size = self.archive()
        result = self.run_install(archive, digest, size)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(profile.read_text().startswith("# user configuration\nexport KEEP_ME=1\n"))
        self.assertEqual(profile.read_text().count("SpeechCraft user command PATH"), 2)
        self.assertEqual(self.run_install(archive, digest, size).returncode, 0)
        self.assertEqual(profile.read_text().count("SpeechCraft user command PATH"), 2)

        other_home = self.root / "symlink-home"
        other_home.mkdir()
        target = self.root / "external-profile"
        target.write_text("leave this alone\n")
        (other_home / ".zprofile").symlink_to(target)
        env = {**os.environ, "HOME": str(other_home), "PATH": f"{self.mockbin}:{os.environ['PATH']}"}
        refused = subprocess.run(["sh", str(INSTALLER), str(archive), digest, size, "a" * 40],
                                 env=env, text=True, capture_output=True)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(".zprofile is a symlink", refused.stderr)
        self.assertFalse((other_home / "Library/Application Support/SpeechCraft/app/current").exists())
        self.assertEqual(target.read_text(), "leave this alone\n")


if __name__ == "__main__":
    unittest.main()
