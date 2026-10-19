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
        archive_name = "speechcraft-0.1.1-linux-x86_64.tar.gz"
        (self.assets / archive_name).write_bytes(archive)
        (self.assets / f"{archive_name}.sha256").write_text(f"{digest(archive)}  {archive_name}\n")
        manifest = {"schema_version": 1, "archive": archive_name, "archive_size_bytes": len(archive),
                    "archive_sha256": digest(archive), "application_version": "0.1.1",
                    "platform": "linux-x86_64", "source_revision": source_revision,
                    "model_manager_wheel_sha256": "a" * 64}
        (self.assets / f"{archive_name}.release.json").write_text(json.dumps(manifest) + "\n")
        mac_archive = b"test-only mac package bytes"
        mac_name = "speechcraft-0.1.1-macos-arm64.tar.gz"
        (self.assets / mac_name).write_bytes(mac_archive)
        (self.assets / f"{mac_name}.sha256").write_text(f"{digest(mac_archive)}  {mac_name}\n")
        mac_manifest = {"schema_version": 1, "archive": mac_name, "archive_size_bytes": len(mac_archive),
                        "archive_sha256": digest(mac_archive), "application_version": "0.1.1",
                        "platform": "macos-arm64", "source_revision": source_revision}
        (self.assets / f"{mac_name}.release.json").write_text(json.dumps(mac_manifest) + "\n")
        (self.assets / "install-linux.py").write_text("# verified test fixture\n")
        (self.assets / "install-linux.sh").write_text("#!/bin/sh\nprintf installed > \"$INSTALL_MARKER\"\n")
        (self.assets / "install-macos.sh").write_text("#!/bin/sh\nprintf mac-installed > \"$INSTALL_MARKER\"\n")
        (self.mockbin / "plutil").write_text("#!/bin/sh\n[ \"$1\" = -extract ] || exit 2\npython3 - \"$2\" \"$6\" <<'PY'\nimport json,sys\nx=json.load(open(sys.argv[2]));\nfor k in sys.argv[1].split('.'): x=x[k]\nprint(str(x).lower() if isinstance(x,bool) else x)\nPY\n")
        os.chmod(self.mockbin / "plutil", 0o755)
        self.curl_log = self.root / "curl.log"
        self.script = self.root / "install.sh"
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1", "--source-revision", source_revision,
                        "--output", str(self.assets)], cwd=ROOT, check=True, capture_output=True)
        shutil.copyfile(self.assets / "install.sh", self.script)
        (self.mockbin / "curl").write_text("""#!/bin/sh
set -eu
if [ -n "${MOCK_CURL_LOG:-}" ]; then printf '%s\\n' "$*" >> "$MOCK_CURL_LOG"; fi
out=
url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --output) out=$2; shift 2 ;;
    --*) shift ;;
    *) url=$1; shift ;;
  esac
done
if [ "${MOCK_CURL_FAIL:-}" = 1 ]; then printf partial > "$out"; exit 28; fi
if [ "${MOCK_CURL_HTTP_STATUS:-}" = 404 ]; then exit 22; fi
name=${url##*/}
cp "$TEST_ASSET_DIR/$name" "$out"
case "${MOCK_EFFECTIVE_URL:-}" in
  __REQUEST_URL__) printf '%s' "$url" ;;
  __ATTACKER__) printf 'http://127.0.0.1:18765.attacker.example/%s' "$name" ;;
  *) printf 'https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/%s' "$name" ;;
esac
""")
        os.chmod(self.mockbin / "curl", 0o755)
        (self.mockbin / "uname").write_text("#!/bin/sh\ncase \"$1\" in -s) printf '%s\\n' \"${UNAME_VALUE:-Linux}\" ;; -m) printf '%s\\n' \"${UNAME_MACHINE:-x86_64}\" ;; -r) printf '%s\\n' \"${UNAME_RELEASE:-7.2.5-linux}\" ;; *) exit 1 ;; esac\n")
        os.chmod(self.mockbin / "uname", 0o755)
        (self.mockbin / "id").write_text("#!/bin/sh\n[ \"$1\" = -u ] || exit 2\nprintf '%s\\n' \"${MOCK_UID:-1000}\"\n")
        os.chmod(self.mockbin / "id", 0o755)
        (self.mockbin / "sed").write_text("""#!/bin/sh
if [ "${3:-}" = /etc/os-release ] && [ -n "${MOCK_OS_RELEASE_ID:-}${MOCK_OS_RELEASE_VERSION:-}" ]; then
  case "$2" in
    s/^ID=//p) printf '%s\\n' "${MOCK_OS_RELEASE_ID:-ubuntu}" ;;
    s/^VERSION_ID=//p) printf '%s\\n' "${MOCK_OS_RELEASE_VERSION:-24.04}" ;;
    *) exit 2 ;;
  esac
else
  exec /bin/sed "$@"
fi
""")
        os.chmod(self.mockbin / "sed", 0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_bootstrap(self, **extra):
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.tmpdir),
               "TEST_ASSET_DIR": str(self.assets), "INSTALL_MARKER": str(self.marker),
               "MOCK_CURL_LOG": str(self.curl_log),
               "PATH": f"{self.mockbin}:{os.environ['PATH']}", **extra}
        return subprocess.run(["sh", str(self.script)], env=env, text=True, capture_output=True, timeout=30)

    def assert_clean_tmp(self):
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_pinned_release_installs_and_cleans_temporary_files(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(), "installed")
        self.assertIn("SpeechCraft 0.1.1 is installed", result.stderr)
        self.assertNotIn("Fetching", result.stdout)
        self.assert_clean_tmp()

    def test_noninteractive_install_prints_stages_without_progress_spam_or_urls(self):
        result = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stderr
        self.assertNotIn("Fetching", result.stdout)
        archive = "speechcraft-0.1.1-linux-x86_64.tar.gz"
        self.assertLess(text.index("Checking platform and prerequisites"), text.index(f"Fetching {archive}"))
        self.assertLess(text.index(f"Fetching {archive}"), text.index(f"Verified SHA-256 for {archive}"))
        self.assertLess(text.index(f"Verified SHA-256 for {archive}"), text.index("Verified release manifest"))
        self.assertLess(text.index("Verified release manifest"), text.index("SpeechCraft 0.1.1 is installed"))
        self.assertIn("Platform checks passed: Linux x86-64", text)
        self.assertIn("[ -t 2 ]", self.script.read_text())
        self.assertIn(f"Fetching {archive} (", text)
        self.assertIn("bytes expected)", text)
        for name in (archive, f"{archive}.sha256", f"{archive}.release.json", "install-linux.py", "install-linux.sh"):
            self.assertIn(f"Fetching {name}", text)
            self.assertIn(f"Fetched {name} (", text)
        self.assertNotIn("%", text)
        self.assertNotIn("github.com", text + result.stderr)
        self.assertNotIn("token", (text + result.stderr).lower())
        lines = self.curl_log.read_text().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(all("--silent" in line and "--progress-bar" not in line for line in lines))
        self.assert_clean_tmp()

    def test_verbose_archive_fetch_requests_progress_for_the_archive_only(self):
        result = self.run_bootstrap(SPEECHCRAFT_INSTALL_VERBOSE="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.curl_log.read_text().splitlines()

        def url_of(line):
            return line.split()[-1]

        archive_lines = [line for line in lines if url_of(line).endswith("-linux-x86_64.tar.gz")]
        other_lines = [line for line in lines if line not in archive_lines]
        self.assertEqual(len(archive_lines), 1)
        self.assertIn("--progress-bar", archive_lines[0])
        self.assertNotIn("--silent", archive_lines[0])
        self.assertEqual(len(other_lines), 4)
        self.assertTrue(all("--silent" in line and "--progress-bar" not in line for line in other_lines))
        self.assertNotIn("github.com", result.stdout + result.stderr)
        self.assert_clean_tmp()

    def test_numbered_internal_release_tag_is_bound_to_application_version(self):
        output = self.root / "internal"
        output.mkdir()
        for item in self.assets.iterdir():
            if item.name != "install.sh":
                shutil.copyfile(item, output / item.name)
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1-internal.1",
                        "--source-revision", source_revision, "--output", str(output)],
                       cwd=ROOT, check=True, capture_output=True)
        self.assertIn("RELEASE_TAG='v0.1.1-internal.1'", (output / "install.sh").read_text())
        self.assertIn("APP_VERSION='0.1.1'", (output / "install.sh").read_text())

    def test_generated_bootstrap_has_independent_release_pins_for_both_platforms(self):
        text = self.script.read_text()
        self.assertIn("LINUX_ARCHIVE='speechcraft-0.1.1-linux-x86_64.tar.gz'", text)
        self.assertIn("MACOS_ARCHIVE='speechcraft-0.1.1-macos-arm64.tar.gz'", text)
        self.assertRegex(text, r"LINUX_ARCHIVE_SHA256='[a-f0-9]{64}'")
        self.assertRegex(text, r"MACOS_ARCHIVE_SHA256='[a-f0-9]{64}'")
        self.assertIn("INSTALL_MACOS_SH_SHA256=", text)

    def test_legacy_linux_release_generator_keeps_linux_working_and_rejects_mac(self):
        output = self.root / "linux-only"
        output.mkdir()
        versioned = "speechcraft-0.1.1-linux-x86_64.tar.gz"
        for name in (versioned, f"{versioned}.sha256", f"{versioned}.release.json", "install-linux.py", "install-linux.sh"):
            shutil.copyfile(self.assets / name, output / name)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1", "--source-revision", revision,
                        "--output", str(output), "--linux-only"], cwd=ROOT, check=True, capture_output=True)
        script = output / "install.sh"
        self.assertIn("MACOS_ARCHIVE=''", script.read_text())
        shutil.copyfile(script, self.script)
        rejected = self.run_bootstrap(UNAME_VALUE="Darwin", UNAME_MACHINE="arm64")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("does not include a macOS ARM64 package", rejected.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_darwin_arm64_selects_only_pinned_mac_assets(self):
        result = self.run_bootstrap(UNAME_VALUE="Darwin", UNAME_MACHINE="arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(), "mac-installed")
        lines = self.curl_log.read_text().splitlines()
        requested = [line.split()[-1].rsplit('/', 1)[-1] for line in lines]
        self.assertEqual(len(requested), 4)
        self.assertIn("speechcraft-0.1.1-macos-arm64.tar.gz", requested)
        self.assertIn("install-macos.sh", requested)
        self.assertFalse(any("linux-x86_64" in name or "install-linux" in name for name in requested))
        self.assertIn("Apple Silicon macOS", result.stderr)

    def test_darwin_intel_and_unknown_architectures_fail_before_download(self):
        result = self.run_bootstrap(UNAME_VALUE="Darwin", UNAME_MACHINE="x86_64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Intel Mac is unsupported", result.stderr)
        self.assertFalse(self.curl_log.exists())
        result = self.run_bootstrap(UNAME_VALUE="Darwin", UNAME_MACHINE="armv7")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported platform Darwin/armv7", result.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_root_and_wsl1_fail_before_any_release_download(self):
        result = self.run_bootstrap(MOCK_UID="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ordinary non-root user, without sudo', result.stderr)
        self.assertFalse(self.curl_log.exists())
        result = self.run_bootstrap(UNAME_RELEASE="4.4.0-19041-Microsoft")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WSL1 is not supported', result.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_wsl2_unsupported_guest_fails_before_any_release_download(self):
        result = self.run_bootstrap(UNAME_RELEASE="6.6.87.2-microsoft-standard-WSL2",
                                   MOCK_OS_RELEASE_ID="fedora", MOCK_OS_RELEASE_VERSION="42")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WSL2 guest', result.stderr)
        self.assertFalse(self.curl_log.exists())

    def test_corrupt_mac_archive_is_rejected_before_mac_installer(self):
        archive = self.assets / "speechcraft-0.1.1-macos-arm64.tar.gz"
        archive.write_bytes(b"changed Mac payload")
        result = self.run_bootstrap(UNAME_VALUE="Darwin", UNAME_MACHINE="arm64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("archive verification failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_loopback_staging_bootstrap_downloads_exact_pinned_urls(self):
        output = self.root / "staged"
        output.mkdir()
        for item in self.assets.iterdir():
            if item.name != "install.sh":
                shutil.copyfile(item, output / item.name)
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1-internal.1", "--source-revision", source_revision,
                        "--asset-base-url", "http://127.0.0.1:18765", "--output", str(output)],
                       cwd=ROOT, check=True, capture_output=True)
        script = output / "install.sh"
        self.assertIn("ASSET_BASE_URL='http://127.0.0.1:18765'", script.read_text())
        shutil.copyfile(script, self.script)
        result = self.run_bootstrap(MOCK_EFFECTIVE_URL="__REQUEST_URL__")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.marker.read_text(), "installed")
        self.assertNotIn("18765", result.stdout + result.stderr)
        self.assert_clean_tmp()

    def test_generator_rejects_non_loopback_http_staging(self):
        output = self.root / "remote-http"
        output.mkdir()
        source_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        result = subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1-internal.1", "--source-revision", source_revision,
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
        subprocess.run(["node", str(GENERATOR), "--tag", "v0.1.1-internal.1", "--source-revision", source_revision,
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
        self.assertIn("failed at SHA-256 check", result.stderr)
        self.assertIn("run the installer again", result.stderr)
        self.assertNotIn("Verified SHA-256", result.stderr)
        self.assertNotIn("Verified SHA-256", result.stdout)
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
        manifest = self.assets / "speechcraft-0.1.1-linux-x86_64.tar.gz.release.json"
        manifest.write_text(manifest.read_text() + " ")
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release manifest verification failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_manifest_identity_mismatch_is_rejected_even_with_matching_hash_pin(self):
        manifest_path = self.assets / "speechcraft-0.1.1-linux-x86_64.tar.gz.release.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["source_revision"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest) + "\n")
        content_hash = digest(manifest_path.read_bytes())
        self.script.write_text(self.script.read_text().replace(
            next(line for line in self.script.read_text().splitlines() if line.startswith("LINUX_MANIFEST_SHA256=")),
            f"LINUX_MANIFEST_SHA256='{content_hash}'"))
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release metadata mismatch", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_unsupported_os_and_architecture_fail_before_download(self):
        result = self.run_bootstrap(UNAME_VALUE="Darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Intel Mac is unsupported", result.stderr)
        self.assertNotIn("Platform checks passed", result.stderr)
        self.assert_clean_tmp()
        (self.mockbin / "uname").write_text("#!/bin/sh\nif [ \"$1\" = -s ]; then printf 'Linux\\n'; else printf 'aarch64\\n'; fi\n")
        os.chmod(self.mockbin / "uname", 0o755)
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported platform Linux/aarch64", result.stderr)
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
                             ("uname", str(self.mockbin / "uname")),
                             ("id", str(self.mockbin / "id"))):
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
        self.assertIn("failed at fetching", result.stderr)
        self.assertIn("speechcraft-0.1.1-linux-x86_64.tar.gz", result.stderr)
        self.assertIn("run the installer again", result.stderr)
        self.assertNotIn("http://", result.stderr)
        self.assertNotIn("https://", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_unavailable_release_asset_404_fails_and_cleans_up(self):
        result = self.run_bootstrap(MOCK_CURL_HTTP_STATUS="404")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("download failed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_generated_installer_parses_as_posix_shell(self):
        result = subprocess.run(["bash", "--posix", "-n", str(self.script)], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pinned_sidecar_size_rejects_a_different_length(self):
        import re
        text = self.script.read_text()
        self.assertRegex(text, r"CHECKSUM_SIZE='[0-9]+'")
        self.script.write_text(re.sub(r"CHECKSUM_SIZE='[0-9]+'", "CHECKSUM_SIZE='1'", text, count=1))
        result = self.run_bootstrap()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("release checksum verification failed", result.stderr)
        self.assertNotIn("Verified SHA-256 for speechcraft-0.1.1-linux-x86_64.tar.gz.sha256", result.stderr)
        self.assertNotIn("is installed", result.stderr)
        self.assertFalse(self.marker.exists())
        self.assert_clean_tmp()

    def test_terminal_stderr_requests_archive_progress_while_stdout_stays_quiet(self):
        import pty
        import threading
        master, slave = pty.openpty()
        chunks = []

        def drain():
            try:
                while True:
                    data = os.read(master, 4096)
                    if not data:
                        break
                    chunks.append(data)
            except OSError:
                return

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        env = {**os.environ, "HOME": str(self.home), "TMPDIR": str(self.tmpdir),
               "TEST_ASSET_DIR": str(self.assets), "INSTALL_MARKER": str(self.marker),
               "MOCK_CURL_LOG": str(self.curl_log),
               "PATH": f"{self.mockbin}:{os.environ['PATH']}"}
        result = subprocess.run(["sh", str(self.script)], env=env, text=True, stdout=subprocess.PIPE,
                                stderr=slave, timeout=30)
        os.close(slave)
        thread.join(timeout=5)
        os.close(master)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Fetching", result.stdout)
        self.assertNotIn("is installed", result.stdout)
        shown = b"".join(chunks).decode(errors="replace")
        self.assertIn("Fetching speechcraft-0.1.1-linux-x86_64.tar.gz", shown)
        self.assertIn("SpeechCraft 0.1.1 is installed", shown)
        lines = self.curl_log.read_text().splitlines()
        archive_lines = [line for line in lines if line.split()[-1].endswith("-linux-x86_64.tar.gz")]
        other_lines = [line for line in lines if line not in archive_lines]
        self.assertEqual(len(archive_lines), 1)
        self.assertIn("--progress-bar", archive_lines[0])
        self.assertTrue(all("--silent" in line and "--progress-bar" not in line for line in other_lines))
        self.assertEqual(self.marker.read_text(), "installed")
        self.assert_clean_tmp()


if __name__ == "__main__":
    unittest.main()
