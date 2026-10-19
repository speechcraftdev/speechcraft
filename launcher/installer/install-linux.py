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

MANDATORY_RUNTIME_FILES = {
    "setup-controller/src/server.js",
    "setup-controller/resources/runtime-manifest.json",
    "setup-controller/resources/bootstrap-manifest.json",
    "setup-controller/resources/model-manager-requirements.txt",
    "backend/app/main.py",
    "backend/app/dataset_runs.py",
    "backend/app/dataset_worker_client.py",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "workers/dataset/pyproject.toml",
    "workers/dataset/uv.lock",
    "workers/dataset/scripts/preflight.py",
    "workers/dataset/scripts/runtime_probe.py",
    "workers/dataset/speechcraft_dataset/__init__.py",
    "workers/dataset/runtime-fixtures/speech.wav",
    "workers/dataset/runtime-fixtures/speech-silence.wav",
    "workers/dataset/runtime-fixtures/speech-compressed.mp3",
    "workers/dataset/runtime-fixtures/community1-two-voice.wav",
    "workers/dataset/runtime-profiles/linux-cpu/pyproject.toml",
    "workers/dataset/runtime-profiles/linux-cpu/uv.lock",
    "workers/dataset/model-catalog.json",
    "workers/dataset/model-manifest.schema.json",
}
MANDATORY_APP_FILES = {"runtime/node", "bin/speechcraft"}


def fail(message):
    raise SystemExit(f"speechcraft installer: {message}")


def note(message):
    print(f"SpeechCraft installer: {message}", flush=True)


def progress_checkpoints(total):
    if not isinstance(total, int) or total < 1:
        return ()
    half = total // 2
    if 0 < half < total:
        return (half, total)
    return (total,)


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


def inspect_command(command, app_base):
    if not command.exists() and not command.is_symlink():
        return None
    if command.is_symlink():
        target = command.resolve(strict=False)
        if not str(target).startswith(str(app_base.resolve(strict=False)) + os.sep):
            fail(f"refusing to replace unrelated command: {command}")
        return {"kind": "symlink", "target": os.readlink(command)}
    info = os.lstat(command)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        fail(f"refusing to replace unrelated command: {command}")
    content = command.read_bytes()
    expected = f'#!/bin/sh\n# SpeechCraft per-user command wrapper\nexec "{app_base}/current/bin/speechcraft" "$@"\n'.encode("utf8")
    if content != expected:
        fail(f"refusing to replace unrelated command: {command}")
    return {"kind": "file", "content": content, "mode": stat.S_IMODE(info.st_mode)}


def restore_command(command, prior):
    if prior is None:
        try:
            command.unlink()
        except FileNotFoundError:
            pass
        return
    temporary = command.parent / f".speechcraft-rollback-{os.getpid()}"
    try:
        if prior["kind"] == "symlink":
            temporary.symlink_to(prior["target"])
        else:
            temporary.write_bytes(prior["content"])
            os.chmod(temporary, prior["mode"])
        os.replace(temporary, command)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


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
    note("archive SHA-256 verified")
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
    bin_dir = home / ".local/bin"
    command = bin_dir / "speechcraft"
    local_base = home / ".local"
    for directory in (local_base, local_base / "share", local_base / "share/speechcraft", bin_dir, app_base, releases):
        if directory.exists() or directory.is_symlink():
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                fail(f"installation destination is not a real directory owned by this user: {directory}")
    # Preflight the command collision and current pointer before extraction or
    # release publication. A failed command registration must never activate
    # a new current release.
    prior_command = inspect_command(command, app_base)
    old_current = app_base / "current"
    prior_current_target = None
    if old_current.exists() or old_current.is_symlink():
        if not old_current.is_symlink():
            fail("active application pointer is not a symlink; refusing to replace it")
        prior_current_target = os.readlink(old_current)
        resolved_current = old_current.resolve(strict=False)
        if not str(resolved_current).startswith(str(releases.resolve(strict=False)) + os.sep):
            fail("active application pointer escapes the versioned release directory")
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
            file_total = sum(1 for member in members if member.isfile())
            note(f"extracting {archive.name} ({file_total} files)")
            extracted_files = 0
            extract_marks = set(progress_checkpoints(file_total))
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
                    extracted_files += 1
                    if extracted_files in extract_marks:
                        note(f"extracted {extracted_files} of {file_total} files")
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
            total_records = len(records)
            note(f"verifying {total_records} packaged files")
            verified_files = 0
            verify_marks = set(progress_checkpoints(total_records)) - {total_records}
            for record in records:
                rel = safe_relative(record["path"])
                item = app_root.joinpath(*rel.parts)
                if not item.is_file() or item.is_symlink() or item.stat().st_size != record.get("size_bytes") or digest_file(item) != record.get("sha256"):
                    fail(f"release file verification failed: {record['path']}")
                verified_files += 1
                if verified_files in verify_marks:
                    note(f"verified {verified_files} of {total_records} packaged files")
            runtime_inventory = manifest.get("runtime_inventory")
            declared_runtime = runtime_inventory.get("required_files") if isinstance(runtime_inventory, dict) else None
            if runtime_inventory.get("status") != "complete" or not isinstance(declared_runtime, list):
                fail("release runtime resource inventory is missing or incomplete")
            runtime_manifest_path = app_root / "setup-controller/resources/runtime-manifest.json"
            try:
                runtime_manifest = json.loads(runtime_manifest_path.read_text("utf8"))
            except (OSError, json.JSONDecodeError):
                fail("trusted runtime manifest is missing or invalid")
            references = {runtime_manifest.get("worker", {}).get("validation_helper")}
            references.update(probe.get("helper") for probe in runtime_manifest.get("runtime_validation", {}).get("probes", []))
            references.update(fixture.get("path") for fixture in runtime_manifest.get("runtime_validation", {}).get("fixtures", {}).values())
            if None in references:
                fail("trusted runtime manifest contains an incomplete resource reference")
            for reference in references:
                safe_relative(reference)
            required = MANDATORY_RUNTIME_FILES | references
            if not required.issubset(set(declared_runtime)) or not required.issubset(expected_names) \
                    or not MANDATORY_APP_FILES.issubset(expected_names):
                missing = sorted(required - set(expected_names))
                missing.extend(sorted(MANDATORY_APP_FILES - set(expected_names)))
                fail(f"application archive omits mandatory runtime resources: {', '.join(missing)}")
            node = app_root / "runtime/node"
            if not node.is_file() or not os.access(node, os.X_OK) or digest_file(node) != manifest["node"]["sha256"]:
                fail("bundled Node executable failed identity verification")
            try:
                version_result = subprocess.run([str(node), "--version"], check=True, capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                fail("bundled Node executable could not be started")
            if version_result.stdout.strip() != f"v{manifest['node']['version']}":
                fail("bundled Node version does not match the release manifest")
            note(f"bundled Node.js {manifest['node']['version']} matches the release")
            note(f"verified {verified_files} of {total_records} packaged files")
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
            # Release contents are immutable at runtime. Publish the new
            # versioned directory first, but do not activate it yet.
            note(f"setting read-only permissions on {release_name}")
            for root, dirs, files in os.walk(destination, topdown=False, followlinks=False):
                for name in files:
                    item = pathlib.Path(root) / name
                    os.chmod(item, 0o555 if item.stat().st_mode & 0o111 else 0o444)
                for name in dirs:
                    os.chmod(pathlib.Path(root) / name, 0o555)
            os.chmod(destination, 0o555)
            bin_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            if command.exists() or command.is_symlink():
                # Recheck immediately before mutation in case another program
                # appeared after preflight.
                inspect_command(command, app_base)
            marker = "# SpeechCraft per-user command wrapper"
            script = f"#!/bin/sh\n{marker}\nexec \"{app_base}/current/bin/speechcraft\" \"$@\"\n"
            note("activating the launcher command")
            temp_command = bin_dir / f".speechcraft-{os.getpid()}"
            current_tmp = app_base / f".current-{os.getpid()}"
            activated_command = False
            try:
                temp_command.write_text(script, encoding="utf8")
                os.chmod(temp_command, 0o755)
                os.replace(temp_command, command)
                activated_command = True
                current_tmp.symlink_to(pathlib.Path("releases") / release_name)
                os.replace(current_tmp, old_current)
                if old_current.resolve(strict=True) != destination.resolve(strict=True):
                    raise OSError("active release pointer verification failed")
            except Exception as activation_error:
                try:
                    if activated_command:
                        restore_command(command, prior_command)
                    if prior_current_target is None:
                        if old_current.is_symlink():
                            old_current.unlink()
                    else:
                        rollback_pointer = app_base / f".current-rollback-{os.getpid()}"
                        rollback_pointer.symlink_to(prior_current_target)
                        os.replace(rollback_pointer, old_current)
                except Exception as rollback_error:
                    fail(f"activation failed ({activation_error}); rollback could not be confirmed ({rollback_error})")
                fail(f"activation failed and previous command/release state was restored: {activation_error}")
            finally:
                for temporary in (temp_command, current_tmp):
                    if temporary.exists() or temporary.is_symlink():
                        temporary.unlink()
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
