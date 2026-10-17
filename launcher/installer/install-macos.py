#!/usr/bin/env python3
"""Install a verified SpeechCraft macOS ARM64 archive for the current user."""
import hashlib
import json
import os
import pathlib
import stat
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


def host_identity():
    override = os.environ.get("SPEECHCRAFT_INSTALL_PLATFORM_TEST", "")
    if override:
        system, machine = override.split(":", 1)
        return system, machine
    return sys.platform, os.uname().machine


def install_archive(archive_arg, home):
    system, machine = host_identity()
    if system != "darwin" or machine not in {"arm64", "aarch64"}:
        fail("this archive is only for macOS Apple Silicon")
    archive = pathlib.Path(archive_arg).resolve(strict=True)
    checksum = pathlib.Path(str(archive) + ".sha256")
    release_file = pathlib.Path(str(archive) + ".release.json")
    if not checksum.is_file() or checksum.is_symlink() or not release_file.is_file() or release_file.is_symlink():
        fail("adjacent .sha256 and .release.json files are required")
    expected = checksum.read_text("ascii").split()[0]
    if len(expected) != 64 or digest_file(archive) != expected:
        fail("archive SHA-256 verification failed")
    release = json.loads(release_file.read_text("utf8"))
    if release.get("schema_version") != 1 or release.get("platform") != "macos-arm64" or release.get("archive_sha256") != expected:
        fail("external release manifest does not match the macOS archive")
    if release.get("signing", {}).get("status") == "notarized":
        fail("refusing a manifest that claims notarization without a signed ticket in the archive")
    root = pathlib.Path(home).resolve()
    app_base = root / "Library/Application Support/SpeechCraft/app"
    releases = app_base / "releases"
    bin_dir = root / "Library/Application Support/SpeechCraft/bin"
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage = pathlib.Path(tempfile.mkdtemp(prefix=".install-", dir=releases))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                path = pathlib.PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    fail(f"unsafe archive member: {member.name}")
                target = stage.joinpath(*path.parts)
                if not target.resolve().is_relative_to(stage.resolve()):
                    fail(f"archive path escaped staging: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    source = tar.extractfile(member)
                    if source is None:
                        fail(f"missing archive member: {member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source, open(target, "xb") as output:
                        output.write(source.read())
                    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
        children = [path for path in stage.iterdir()]
        if len(children) != 1 or not children[0].is_dir():
            fail("archive must contain one application directory")
        release_name = children[0].name
        published = releases / release_name
        if published.exists():
            fail(f"release already exists: {published}")
        os.replace(children[0], published)
        command = bin_dir / "speechcraft"
        staged_command = bin_dir / ".speechcraft-next"
        if staged_command.exists() or staged_command.is_symlink():
            staged_command.unlink()
        staged_command.write_text(f'#!/bin/sh\nexec "{published}/bin/speechcraft" "$@"\n', encoding="utf8")
        os.chmod(staged_command, 0o755)
        zshrc = root / ".zshrc"
        marker = "# SpeechCraft user command"
        path_line = f'export PATH="$HOME/Library/Application Support/SpeechCraft/bin:$PATH"'
        existing = zshrc.read_text("utf8") if zshrc.is_file() and not zshrc.is_symlink() else ""
        if marker not in existing:
            addition = f"\n{marker}\n{path_line}\n"
            with open(zshrc, "a", encoding="utf8") as shell_config:
                shell_config.write(addition)
        current = app_base / "current"
        previous = os.readlink(current) if current.is_symlink() else None
        temporary = app_base / ".current-next"
        if temporary.is_symlink() or temporary.exists():
            temporary.unlink()
        temporary.symlink_to(published)
        os.replace(temporary, current)
        try:
            os.replace(staged_command, command)
        except Exception:
            if previous:
                current.unlink()
                current.symlink_to(previous)
            elif current.is_symlink() or current.exists():
                current.unlink()
            raise
    except Exception:
        if stage.exists():
            for child in stage.iterdir():
                if child.is_dir():
                    pass
        raise
    finally:
        if stage.exists():
            for child in list(stage.iterdir()):
                if child.is_file() or child.is_symlink():
                    child.unlink()
            try:
                stage.rmdir()
            except OSError:
                pass
    return {"command": str(bin_dir / "speechcraft"), "current": str(app_base / "current")}


def main(argv):
    if len(argv) != 2:
        fail("usage: install-macos.py ARCHIVE.tar.gz")
    home = os.environ.get("SPEECHCRAFT_INSTALL_HOME_TEST") or str(pathlib.Path.home())
    install_archive(argv[1], home)
    print("Installed SpeechCraft for this user. Open a new terminal if the bin directory is not on PATH.")


if __name__ == "__main__":
    main(sys.argv)
