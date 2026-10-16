#!/usr/bin/env python3
"""Verify and atomically install a SpeechCraft Linux application archive."""
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


def fail(message):
    raise SystemExit(f"speechcraft installer: {message}")


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(raw):
    path = pathlib.PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        fail(f"unsafe archive path: {raw!r}")
    return path


def main(archive_arg):
    if sys.platform != "linux" or os.uname().machine not in ("x86_64", "amd64"):
        fail("this archive is only for Linux x86-64")
    archive = pathlib.Path(archive_arg).resolve(strict=True)
    checksum = pathlib.Path(str(archive) + ".sha256")
    release_file = pathlib.Path(str(archive) + ".release.json")
    if not checksum.is_file() or checksum.is_symlink() or not release_file.is_file() or release_file.is_symlink():
        fail("adjacent .sha256 and .release.json files are required")
    expected = checksum.read_text("ascii").split()[0]
    if len(expected) != 64 or digest_file(archive) != expected:
        fail("archive SHA-256 verification failed")
    release = json.loads(release_file.read_text("utf8"))
    if release.get("schema_version") != 1 or release.get("archive_sha256") != expected \
            or release.get("archive_size_bytes") != archive.stat().st_size or release.get("archive") != archive.name \
            or release.get("platform") != "linux-x86_64":
        fail("external release manifest does not match the archive")

    home = pathlib.Path.home().resolve(strict=True)
    if sys.version_info < (3, 9):
        fail("the local archive installer needs Python 3.9 or newer; SpeechCraft runtime Python is managed separately")
    app_base = home / ".local/share/speechcraft/app"
    releases = app_base / "releases"
    app_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (app_base, releases):
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            fail(f"application install directory is not a real directory owned by this user: {directory}")
    stage_parent = releases
    stage = pathlib.Path(tempfile.mkdtemp(prefix=".install-", dir=stage_parent))
    os.chmod(stage, 0o700)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            if not members or len(members) > 10_000 or sum(max(0, member.size) for member in members) > 1024 * 1024 * 1024:
                fail("archive is empty or exceeds the installation size/member limits")
            top = None
            extracted = {}
            for member in members:
                rel = safe_relative(member.name)
                if top is None:
                    top = rel.parts[0]
                if rel.parts[0] != top or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    fail(f"unsupported or unsafe archive member: {member.name}")
                target = stage.joinpath(*rel.parts)
                if not target.resolve().is_relative_to(stage.resolve()):
                    fail(f"archive path escaped staging directory: {member.name}")
                if member.isfile():
                    source = tar.extractfile(member)
                    if source is None:
                        fail(f"could not read archive member: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, open(target, "xb") as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                    extracted["/".join(rel.parts[1:])] = target
                else:
                    target.mkdir(parents=True, exist_ok=True)
            app_root = stage / top
            manifest_path = app_root / "application-release-manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                fail("application release manifest is missing")
            manifest = json.loads(manifest_path.read_text("utf8"))
            if manifest.get("schema_version") != 1 or manifest.get("platform") != "linux-x86_64":
                fail("unsupported application release manifest")
            if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != release.get("application_manifest_sha256") \
                    or manifest.get("version") != release.get("application_version") \
                    or manifest.get("node", {}).get("version") != release.get("node_version"):
                fail("internal and external application release identities do not agree")
            version = manifest.get("version")
            if not isinstance(version, str) or not version.replace(".", "").replace("-", "").isalnum():
                fail("invalid application version")
            records = manifest.get("package_files")
            if not isinstance(records, list) or not records:
                fail("release file inventory is empty")
            expected_names = {record.get("path") for record in records}
            actual_names = set(extracted) - {"application-release-manifest.json"}
            if actual_names != expected_names:
                fail("archive file inventory differs from the release manifest")
            for record in records:
                rel = safe_relative(record["path"])
                item = app_root.joinpath(*rel.parts)
                if not item.is_file() or item.is_symlink() or item.stat().st_size != record.get("size_bytes") or digest_file(item) != record.get("sha256"):
                    fail(f"release file verification failed: {record['path']}")
            node = app_root / "runtime/node"
            if not node.is_file() or not os.access(node, os.X_OK) or digest_file(node) != manifest["node"]["sha256"]:
                fail("bundled Node executable failed identity verification")
            try:
                version_result = subprocess.run([str(node), "--version"], check=True, capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                fail("bundled Node executable could not be started")
            if version_result.stdout.strip() != f"v{manifest['node']['version']}":
                fail("bundled Node version does not match the release manifest")
            release_name = f"{manifest['application'].lower()}-{version}-linux-x86_64"
            destination = releases / release_name
            if destination.exists():
                destination_info = os.lstat(destination)
                if not stat.S_ISDIR(destination_info.st_mode) or stat.S_ISLNK(destination_info.st_mode) or destination_info.st_uid != os.getuid():
                    fail(f"release destination is not a real user-owned directory: {destination}")
                existing_manifest = destination / "application-release-manifest.json"
                if not existing_manifest.is_file() or digest_file(existing_manifest) != digest_file(manifest_path):
                    fail(f"release destination already exists with different contents: {destination}")
                shutil.rmtree(stage)
                stage = None
            else:
                os.rename(app_root, destination)
                shutil.rmtree(stage)
                stage = None
            # Release contents are immutable at runtime. Upgrades publish a
            # sibling version and switch the current pointer atomically.
            for root, dirs, files in os.walk(destination, topdown=False, followlinks=False):
                for name in files:
                    item = pathlib.Path(root) / name
                    os.chmod(item, 0o555 if item.stat().st_mode & 0o111 else 0o444)
                for name in dirs:
                    os.chmod(pathlib.Path(root) / name, 0o555)
            os.chmod(destination, 0o555)
            current_tmp = app_base / f".current-{os.getpid()}"
            current_tmp.symlink_to(pathlib.Path("releases") / release_name)
            os.replace(current_tmp, app_base / "current")

            bin_dir = home / ".local/bin"
            bin_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            command = bin_dir / "speechcraft"
            marker = "# SpeechCraft per-user command wrapper"
            if command.exists() or command.is_symlink():
                if command.is_symlink():
                    resolved = command.resolve(strict=False)
                    if not str(resolved).startswith(str(app_base.resolve()) + os.sep):
                        fail(f"refusing to replace unrelated command: {command}")
                elif not command.is_file() or marker not in command.read_text("utf8", errors="replace"):
                    fail(f"refusing to replace unrelated command: {command}")
            script = f"#!/bin/sh\n{marker}\nexec \"{app_base}/current/bin/speechcraft\" \"$@\"\n"
            temp_command = bin_dir / f".speechcraft-{os.getpid()}"
            temp_command.write_text(script, encoding="utf8")
            os.chmod(temp_command, 0o755)
            os.replace(temp_command, command)
            print(f"Installed {manifest['application']} {version} at {destination}")
            print(f"Command: {command}")
            if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
                profile = home / ".profile"
                marker = "# >>> SpeechCraft user command PATH >>>"
                block = f"{marker}\ncase \":$PATH:\" in *:\"$HOME/.local/bin:\"*) ;; *) PATH=\"$HOME/.local/bin:$PATH\" ;; esac\nexport PATH\n# <<< SpeechCraft user command PATH <<<\n"
                try:
                    if profile.exists() or profile.is_symlink():
                        profile_info = os.lstat(profile)
                        if not stat.S_ISREG(profile_info.st_mode) or stat.S_ISLNK(profile_info.st_mode) or profile_info.st_uid != os.getuid():
                            raise OSError("~/.profile is not a regular user-owned file")
                        prior = profile.read_text("utf8")
                        mode = stat.S_IMODE(profile_info.st_mode)
                    else:
                        prior, mode = "", 0o600
                    if marker not in prior:
                        fd, temporary_profile = tempfile.mkstemp(prefix=".speechcraft-profile-", dir=home)
                        try:
                            with os.fdopen(fd, "w", encoding="utf8") as stream:
                                stream.write(prior + ("\n" if prior and not prior.endswith("\n") else "") + block)
                                stream.flush(); os.fsync(stream.fileno())
                            os.chmod(temporary_profile, mode)
                            os.replace(temporary_profile, profile)
                        finally:
                            if os.path.exists(temporary_profile): os.unlink(temporary_profile)
                    print(f"PATH: registered {bin_dir} in ~/.profile; open a fresh login terminal")
                except OSError as error:
                    print(f"PATH note: add {bin_dir} to PATH for fresh terminals ({error}); installer did not change ~/.profile.")
            else:
                print("PATH: ~/.local/bin is already available")
            print(f"Persistent data remains under {home}/.local/share/speechcraft/data")
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("Usage: install-linux.sh speechcraft-<version>-linux-x86_64.tar.gz")
    main(sys.argv[1])
