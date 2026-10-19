#!/usr/bin/env python3
"""Offline static audit of locked macOS ARM64 wheel tags and source paths."""
import hashlib
import pathlib
import sys
import tomllib
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "setup-controller/resources/runtime-manifest.json"


def fail(message):
    raise SystemExit(f"macOS lock audit failed: {message}")


def mac_arm64_wheel(filename):
    parts = filename[:-4].rsplit("-", 3) if filename.endswith(".whl") else []
    if len(parts) != 4:
        return False
    python_tag, abi_tag, platform_tag = parts[1], parts[2], parts[3]
    python_tags = python_tag.split(".")
    abi_tags = abi_tag.split(".")
    universal_python = any(tag in {"py3", "py312", "cp312"} or tag.startswith("cp312.") for tag in python_tags)
    abi3_python = "abi3" in abi_tags and any(tag.startswith("cp3") and tag[2:].isdigit() and int(tag[2:]) <= 312 for tag in python_tags)
    if not (universal_python or abi3_python):
        return False
    if not any(tag in {"none", "abi3", "cp312"} for tag in abi_tags):
        return False
    for tag in platform_tag.split("."):
        if tag == "any":
            return True
        if not tag.startswith("macosx_"):
            continue
        fields = tag.split("_")
        if len(fields) < 4 or fields[-1] not in {"arm64", "universal2", "universal"}:
            continue
        try:
            major, minor = int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if major < 14 or (major == 14 and minor == 0):
            return True
    return False


def audit_project(project, lock, expected_project_sha, expected_lock_sha, label):
    project_path = ROOT / project
    lock_path = ROOT / lock
    if hashlib.sha256(project_path.read_bytes()).hexdigest() != expected_project_sha:
        fail(f"{label} pyproject hash does not match runtime-manifest.json")
    if hashlib.sha256(lock_path.read_bytes()).hexdigest() != expected_lock_sha:
        fail(f"{label} lockfile hash does not match runtime-manifest.json")
    data = tomllib.loads(lock_path.read_text())
    missing = []
    unsupported = []
    for package in data.get("package", []):
        # Editable root projects are supplied by the application package itself.
        # They intentionally have neither an sdist nor a wheel in uv.lock.
        source = package.get("source", {})
        if source.get("editable") or source.get("virtual") or source.get("directory"):
            continue
        wheels = package.get("wheels", [])
        if wheels and not any(mac_arm64_wheel(pathlib.PurePosixPath(urllib.parse.unquote(item["url"])).name) for item in wheels) and not package.get("sdist"):
            unsupported.append(f"{package.get('name')}=={package.get('version')}")
        if not wheels and not package.get("sdist"):
            missing.append(f"{package.get('name')}=={package.get('version')}")
    if unsupported:
        fail(f"{label} has no CPython 3.12/macOS 14+ ARM64 wheel: {', '.join(unsupported)}")
    if missing:
        fail(f"{label} packages have no source or wheel artifact: {', '.join(missing)}")
    print(f"{label}: {len(data.get('package', []))} locked packages have a macOS ARM64-compatible wheel or source archive")


manifest = tomllib.loads((ROOT / "workers/dataset/runtime-profiles/macos-arm64/pyproject.toml").read_text())
required = manifest.get("tool", {}).get("uv", {}).get("required-environments", [])
if "sys_platform == 'darwin' and platform_machine == 'arm64' and platform_release == '23.0.0'" not in required:
    fail("macOS worker profile must require the macOS 14.0 (Darwin 23.0.0) ARM64 baseline")
for name, raw in (("backend", "backend"), ("macos-arm64 worker", "workers/dataset/runtime-profiles/macos-arm64")):
    recipe = tomllib.loads((ROOT / (raw + "/pyproject.toml" if raw != "backend" else "backend/pyproject.toml")).read_text())
    project = "backend/pyproject.toml" if raw == "backend" else "workers/dataset/runtime-profiles/macos-arm64/pyproject.toml"
    lock = "backend/uv.lock" if raw == "backend" else "workers/dataset/runtime-profiles/macos-arm64/uv.lock"
    profile = None if raw == "backend" else RUNTIME
    if profile:
        runtime = __import__("json").loads(profile.read_text())
        recipe_info = runtime["worker"]["profiles"]["macos-arm64"]
        audit_project(project, lock, recipe_info["project_manifest_sha256"], recipe_info["lock_sha256"], name)
    else:
        runtime = __import__("json").loads(profile.read_text()) if profile else __import__("json").loads(RUNTIME.read_text())
        backend_info = runtime["backend"]
        audit_project(project, lock, backend_info["project_manifest_sha256"], backend_info["lock_sha256"], name)

mac = tomllib.loads((ROOT / "workers/dataset/runtime-profiles/macos-arm64/pyproject.toml").read_text())
sources = mac.get("tool", {}).get("uv", {}).get("sources", {})
expected_paths = {"speechcraft-dataset-worker": "workers/dataset", "speechcraft-model-manager": "packages/model_manager"}
for package, relative in expected_paths.items():
    source = sources.get(package, {}).get("path")
    resolved = (ROOT / "workers/dataset/runtime-profiles/macos-arm64" / source).resolve() if source else None
    if resolved != (ROOT / relative).resolve() or not resolved.is_dir():
        fail(f"macOS local source path for {package} does not resolve to {relative}")
print("macOS worker local source paths resolve inside the pinned application layout")
