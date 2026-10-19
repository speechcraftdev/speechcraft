import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "launcher/scripts/generate-bootstrap.mjs"
TEMPLATE = ROOT / "launcher/installer/install.sh.in"


def digest(value):
    return hashlib.sha256(value).hexdigest()


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="speechcraft-bootstrap-test-")
        self.root = pathlib.Path(self.temp.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.tmpdir = self.root / "tmp"
        self.tmpdir.mkdir()
        self.mockbin = self.root / "mockbin"
        self.mockbin.mkdir()
        self.marker = self.home / "installer-ran"
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        archive = b"test-only package bytes"
        archive_name = "speechcraft-0.1.0-linux-x86_64.tar.gz"
        (self.assets / archive_name).write_bytes(archive)
        (self.assets / f"{archive_name}.sha256").write_text(f"{digest(archive)}  {archive_name}\n")
        manifest = {"schema_version": 1, "archive": archive_name, "archive_size_bytes": len(archive),
                    "archive_sha256": digest(archive), "application_version": "0.1.0",
                    "platform": "linux-x86_64", "source_revision": source_revision,
                    "model_manager_wheel_sha256": "a" * 64}
        (self.assets / f"{archive_name}.release.json").write_text(json.dumps(manifest) + "\n")
        (self.assets / "install-linux.py").write_text("# verified test fixture\n")
        (self.assets / "install-linux.sh").write_text("#!/bin/sh\nprintf installed > \"$INSTALL_MARKER\"\n")
        self.script = self.root / "install.sh"
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.0", "--source-revision", source_revision,
                        "--output", str(self.assets)], cwd=ROOT, check=True, capture_output=True)
        shutil.copyfile(self.assets / "install.sh", self.script)
        (self.mockbin / "curl").write_text("""#!/bin/sh
set -eu
if [ "${MOCK_CURL_FAIL:-}" = 1 ]; then printf partial > "$out"; exit 28; fi
if [ "${MOCK_CURL_HTTP_STATUS:-}" = 404 ]; then exit 22; fi
out=
url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --*) shift ;;
    *) url=$1; shift ;;
  esac
done
name=${url##*/}
cp "$TEST_ASSET_DIR/$name" "$out"
case "${MOCK_EFFECTIVE_URL:-}" in
  __REQUEST_URL__) printf '%s' "$url" ;;
  __ATTACKER__) printf 'http://127.0.0.1:18765.attacker.example/%s' "$name" ;;
  *) printf 'https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.0/%s' "$name" ;;
esac
""")
        os.chmod(self.mockbin / "curl", 0o755)
        (self.mockbin / "uname").write_text("#!/bin/sh\nif [ \"$1\" = -s ]; then printf '%s\\n' \"${UNAME_VALUE:-Linux}\"; else printf 'x86_64\\n'; fi\n")
        os.chmod(self.mockbin / "uname", 0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_bootstrap(self, **extra):
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.tmpdir),
               "TEST_ASSET_DIR": str(self.assets), "INSTALL_MARKER": str(self.marker),
               "PATH": f"{self.mockbin}:{os.environ['PATH']}", **extra}
        return subprocess.run(["sh", str(self.script)], env=env, text=True, capture_output=True, timeout=30)

    def assert_clean_tmp(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_pinned_release_installs_and_cleans_temporary_files(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(), "installed")
        self.assertIn("SpeechCraft 0.1.0 is installed", result.stdout)
        self.assert_clean_tmp()

    def test_numbered_internal_release_tag_is_bound_to_application_version(self):
        output = self.root / "internal"
        output.mkdir()
        for item in self.assets.iterdir():
            if item.name != "install.sh":
                shutil.copyfile(item, output / item.name)
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.0-internal.1",
                        "--source-revision", source_revision, "--output", str(output)],
                       cwd=ROOT, check=True, capture_output=True)
        self.assertIn("RELEASE_TAG='v0.1.0-internal.1'", (output / "install.sh").read_text())
        self.assertIn("APP_VERSION='0.1.0'", (output / "install.sh").read_text())

    def test_loopback_staging_bootstrap_downloads_exact_pinned_urls(self):
        output = self.root / "staged"
        output.mkdir()
        for item in self.assets.iterdir():
            if item.name != "install.sh":
                shutil.copyfile(item, output / item.name)
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.0-internal.1", "--source-revision", source_revision,
                        "--asset-base-url", "http://127.0.0.1:18765", "--output", str(output)],
                       cwd=ROOT, check=True, capture_output=True)
        script = output / "install.sh"
        self.assertIn("ASSET_BASE_URL='http://127.0.0.1:18765'", script.read_text())
        shutil.copyfile(script, self.script)
        result = self.run_bootstrap(MOCK_EFFECTIVE_URL="__REQUEST_URL__")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(), "installed")
        self.assert_clean_tmp()

    def test_generator_rejects_non_loopback_http_staging(self):
        output = self.root / "remote-http"
        output.mkdir()
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        result = subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.0-internal.1", "--source-revision", source_revision,
                                 "--asset-base-url", "http://staging.example.invalid", "--output", str(output)],
                                cwd=ROOT, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP is allowed only with an explicit loopback port", result.stderr)
        self.assertFalse((output / "install.sh").exists())

    def test_loopback_staging_rejects_redirects_outside_pinned_url(self):
        output = self.root / "staged-redirect"
        output.mkdir()
        for item in self.assets.iterdir():
            if item.name != "install.sh":
                shutil.copyfile(item, output / item.name)
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.0-internal.1", "--source-revision", source_revision,
                        "--asset-base-url", "http://127.0.0.1:18765", "--output", str(output)],
                       cwd=ROOT, check=True, capture_output=True)
        shutil.copyfile(output / "install.sh", self.script)
        result = self.run_bootstrap(MOCK_EFFECTIVE_URL="__ATTACKER__")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("left its pinned URL", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_unexpected_arguments_are_rejected_before_download(self):
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.tmpdir),
               "TEST_ASSET_DIR": str(self.assets), "INSTALL_MARKER": str(self.marker),
               "PATH": f"{self.mockbin}:{os.environ['PATH']}"}
        result = subprocess.run(["sh", str(self.script), "--version"], env=env, text=True,
                                capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("takes no arguments", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_corrupted_archive_fails_closed(self):
        archive = next(self.assets.glob("*-linux-x86_64.tar.gz"))
        archive.write_bytes(b"corrupted archive")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive verification failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_tampered_installer_is_rejected_before_execution(self):
        (self.assets / "install-linux.py").write_text("tampered installer\n")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("official installer verification failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_metadata_bytes_are_pinned(self):
        manifest = next(self.assets.glob("*.release.json"))
        manifest.write_text(manifest.read_text() + " ")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release manifest verification failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_manifest_identity_mismatch_is_rejected_even_with_matching_hash_pin(self):
        manifest_path = next(self.assets.glob("*.release.json"))
        manifest = json.loads(manifest_path.read_text())
        manifest["source_revision"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest) + "\n")
        content_hash = digest(manifest_path.read_bytes())
        self.script.write_text(self.script.read_text().replace(
            next(line for line in self.script.read_text().splitlines() if line.startswith("MANIFEST_SHA256=")),
            f"MANIFEST_SHA256='{content_hash}'"))
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release metadata mismatch", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_unsupported_os_and_architecture_fail_before_download(self):
        result = self.run_bootstrap(UNAME_VALUE="Darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supported platform: Linux x86-64 only", result.stderr)
        self.assert_clean_tmp()
        (self.mockbin / "uname").write_text("#!/bin/sh\nif [ \"$1\" = -s ]; then printf 'Linux\\n'; else printf 'aarch64\\n'; fi\n")
        os.chmod(self.mockbin / "uname", 0o755)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("supported architecture: Linux x86-64 only", result.stderr)
        self.assert_clean_tmp()

    def test_old_system_python_is_explained_before_download(self):
        fake_python = self.mockbin / "python3"
        fake_python.write_text("#!/bin/sh\nif [ \"$1\" = -c ]; then exit 1; fi\nexec /usr/bin/python3 \"$@\"\n")
        os.chmod(fake_python, 0o755)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("system Python 3.9 or newer is required", result.stderr)
        self.assert_clean_tmp()

    def test_missing_mktemp_is_explained_before_download(self):
        limited_path = self.root / "limited-path"
        limited_path.mkdir()
        for name, target in (("python3", os.path.realpath(os.sys.executable)),
                             ("curl", str(self.mockbin / "curl")),
                             ("uname", str(self.mockbin / "uname"))):
            (limited_path / name).symlink_to(target)
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.tmpdir),
               "TEST_ASSET_DIR": str(self.assets), "INSTALL_MARKER": str(self.marker),
               "PATH": str(limited_path)}
        result = subprocess.run(["/bin/sh", str(self.script)], env=env, text=True,
                                capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mktemp is required", result.stderr)
        self.assert_clean_tmp()

    def test_interrupted_download_fails_and_cleans_up(self):
        result = self.run_bootstrap(MOCK_CURL_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_unavailable_release_asset_404_fails_and_cleans_up(self):
        result = self.run_bootstrap(MOCK_CURL_HTTP_STATUS="404")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()


if __name__ == "__main__":
    unittest.main()
