"""Offline model manifest validation; standard library only, no inference.

The checked-in JSON Schema defines structure. Semantic checks below additionally
require unique roles/platforms/paths and canonical relative file paths.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
from importlib.resources import files
import re
import stat
from typing import Any

SCHEMA_PATH = Path(__file__).with_name("model-manifest.schema.json")
SUPPORTED_PLATFORMS = {"linux-x86_64", "macos-arm64", "windows-wsl2"}
PLATFORM_ALIASES = {"windows-wsl2": "linux-x86_64"}


class ManifestError(ValueError):
    """Invalid manifest data, with a location suitable for CLI diagnostics."""


def _schema_check(value: Any, rule: dict, schema: dict, location: str = "manifest") -> None:
    """Evaluate only the JSON Schema keywords used by our authoritative schema.

    Fail closed on new keywords so future schema edits cannot silently weaken
    Python validation. This is not a general JSON Schema implementation.
    """
    supported = {"$schema", "$id", "$defs", "$ref", "$comment", "title", "description",
                 "type", "required", "properties", "additionalProperties", "items",
                 "minItems", "maxItems", "minLength", "minimum", "pattern", "enum", "const"}
    if rule.keys() - supported:
        raise ManifestError(f"{location}: unsupported schema keywords {sorted(rule.keys() - supported)}")
    if "$ref" in rule:
        ref = rule["$ref"]
        if not ref.startswith("#/$defs/"):
            raise ManifestError(f"unsupported schema reference: {ref}")
        _schema_check(value, schema["$defs"][ref.removeprefix("#/$defs/")], schema, location)
        return
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    kind = rule.get("type")
    integer = type(value) is int or (type(value) is float and value.is_integer())
    matches_type = integer if kind == "integer" else kind in types and type(value) is types[kind]
    if kind and not matches_type:
        raise ManifestError(f"{location}: expected {kind}")
    if "const" in rule and (isinstance(value, bool) or value != rule["const"]):
        raise ManifestError(f"{location}: expected {rule['const']!r}")
    if "enum" in rule and value not in rule["enum"]:
        raise ManifestError(f"{location}: expected one of {rule['enum']}")
    if isinstance(value, dict):
        missing = set(rule.get("required", [])) - value.keys()
        if missing:
            raise ManifestError(f"{location}: missing required fields {sorted(missing)}")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ManifestError(f"{location}: unknown fields {sorted(value.keys() - properties.keys())}")
        for key, item in value.items():
            if key in properties:
                _schema_check(item, properties[key], schema, f"{location}.{key}")
    if isinstance(value, list):
        if len(value) < rule.get("minItems", 0) or len(value) > rule.get("maxItems", float("inf")):
            raise ManifestError(f"{location}: invalid item count")
        for index, item in enumerate(value):
            _schema_check(item, rule.get("items", {}), schema, f"{location}[{index}]")
    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            raise ManifestError(f"{location}: string is too short")
        if "pattern" in rule and re.search(rule["pattern"], value) is None:
            raise ManifestError(f"{location}: invalid string format")
    if type(value) in (int, float) and value < rule.get("minimum", value):
        raise ManifestError(f"{location}: below minimum")


def _schema() -> dict:
    return json.loads(files("speechcraft_models").joinpath("model-manifest.schema.json").read_text(encoding="utf-8"))


def _unique(items: list[dict], key: str, location: str) -> None:
    values = [item[key] for item in items]
    if len(values) != len(set(values)):
        raise ManifestError(f"{location}: duplicate {key}")


def _artifact_semantics(artifact: dict) -> None:
    files = artifact["required_files"]
    source = artifact.get("source")
    if source is not None:
        if (source["type"] != "huggingface"
                or re.fullmatch(r"[0-9a-f]{40}", artifact["revision"]) is None
                or any("url" in item for item in files)):
            raise ManifestError("Hugging Face artifacts need a full commit revision and catalog paths, not URLs")
        if source.get("authentication", "public") not in {"public", "gated"}:
            raise ManifestError("Hugging Face artifact authentication must be public or gated")
        if source.get("authentication") == "gated" and artifact["availability"] != "verified":
            raise ManifestError("gated artifacts need a verified immutable identity before installation")
    _unique(files, "path", "artifact.required_files")
    for item in files:
        path = item["path"]
        if (any(part in ("", ".", "..") for part in path.split("/"))
                or any(ord(char) < 32 for char in path) or "\\" in path or ":" in path):
            raise ManifestError(f"unsafe required file path: {path!r}")
        if item.get("min_size_bytes", 0) > item.get("size_bytes", float("inf")):
            raise ManifestError(f"{path}: minimum size exceeds exact size")
    if artifact["inventory_complete"] and not files:
        raise ManifestError("complete inventory must contain files")
    if artifact["platform"] == "all-supported":
        if not artifact.get("supported_platforms"):
            raise ManifestError("shared artifact needs explicit supported_platforms")
        if len(set(artifact["supported_platforms"])) != len(artifact["supported_platforms"]):
            raise ManifestError("duplicate supported_platforms")
    elif "supported_platforms" in artifact:
        raise ManifestError("supported_platforms is only valid for shared artifacts")


def validate_artifact(artifact: Any) -> None:
    schema = _schema()
    _schema_check(artifact, schema["$defs"]["artifact"], schema, "artifact")
    _artifact_semantics(artifact)


def validate_manifest(manifest: Any) -> None:
    schema = _schema()
    _schema_check(manifest, schema, schema)
    _unique(manifest["models"], "role", "models")
    _unique(manifest["models"], "id", "models")
    for model in manifest["models"]:
        _unique(model["artifacts"], "platform", model["id"])
        for artifact in model["artifacts"]:
            _artifact_semantics(artifact)


def load_catalog() -> dict:
    """Read the trusted catalog shipped in this distribution."""
    value = json.loads(files("speechcraft_models").joinpath("model-catalog.json").read_text(encoding="utf-8"))
    validate_manifest(value)
    return value


def parse_manifest(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        value = source
    else:
        try:
            value = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read manifest: {exc}") from exc
    validate_manifest(value)
    return value


def select_artifact(manifest: dict, role: str, platform: str) -> dict:
    """Exact match wins, even if unresolved; otherwise use an explicit shared match."""
    validate_manifest(manifest)
    if platform not in SUPPORTED_PLATFORMS:
        raise ManifestError(f"unsupported platform: {platform}")
    artifact_platform = PLATFORM_ALIASES.get(platform, platform)
    model = next((item for item in manifest["models"] if item["role"] == role), None)
    if model:
        exact = next((a for a in model["artifacts"] if a["platform"] == artifact_platform), None)
        if exact is not None:
            return exact
        shared = next((a for a in model["artifacts"] if a["platform"] == "all-supported"
                       and artifact_platform in a["supported_platforms"]), None)
        if shared is not None:
            return shared
    raise ManifestError(f"no {role} artifact for {platform}")


def _check_file(root_fd: int, required: dict) -> dict:
    """Open every component relative to a directory fd, refusing all symlinks."""
    result = {"path": required["path"]}
    directory = os.dup(root_fd)
    try:
        parts = required["path"].split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return {**result, "status": "unsafe", "reason": "not a regular file"}
            size = before.st_size
            result["size_bytes"] = size
            expected = required.get("size_bytes")
            if size < max(required.get("min_size_bytes", 0), expected or 0):
                return {**result, "status": "partial"}
            if expected is not None and size > expected:
                return {**result, "status": "corrupt", "reason": "size mismatch"}
            if "sha256" not in required:
                return {**result, "status": "present_unverified"}
            hasher = hashlib.sha256()
            for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                hasher.update(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return {**result, "status": "error", "reason": "file changed during verification"}
            digest = hasher.hexdigest()
            return {**result, "sha256": digest,
                    "status": "verified" if digest == required["sha256"] else "corrupt"}
        finally:
            os.close(fd)
    except FileNotFoundError:
        return {**result, "status": "missing"}
    except OSError as exc:
        return {**result, "status": "unsafe" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "error",
                "reason": str(exc)}
    finally:
        os.close(directory)


def verify_artifact(root: str | Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Check declared files; only trusted SHA-256 values can verify contents.

    Callers must supply a trusted manifest. No filesystem report authenticates
    the manifest itself or establishes runtime readiness. Linux/macOS only.
    """
    validate_artifact(artifact)
    details = []
    root_error = None
    exact = None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        details = [{"path": item["path"], "status": "missing"} for item in artifact["required_files"]]
    except OSError as exc:
        root_error = str(exc)
    else:
        try:
            details = [_check_file(root_fd, item) for item in artifact["required_files"]]
            exact = _strict_tree_matches(root_fd, artifact) if artifact.get("strict_tree") else None
        finally:
            os.close(root_fd)
    if root_error is not None or details and all(item["status"] == "missing" for item in details):
        exact = False if artifact.get("strict_tree") else None
    return _report(details, artifact, root, root_error, exact)


def verify_directory_fd(root_fd: int, artifact: dict, root: str = "<directory>") -> dict:
    """Internal store interface: check files beneath an already secured directory."""
    validate_artifact(artifact)
    details = [_check_file(root_fd, item) for item in artifact["required_files"]]
    exact = _strict_tree_matches(root_fd, artifact) if artifact.get("strict_tree") else None
    return _report(details, artifact, root, None, exact)


def _strict_tree_matches(root_fd: int, artifact: dict) -> bool:
    """Require only cataloged files and their implied directories, without following links."""
    expected_files = {item["path"] for item in artifact["required_files"]}
    expected_dirs = {"/".join(parts[:index]) for name in expected_files
                     for parts in [name.split("/")] for index in range(1, len(parts))}
    found_files: set[str] = set()

    def walk(directory_fd: int, prefix: str = "") -> bool:
        for name in os.listdir(directory_fd):
            relative = f"{prefix}/{name}" if prefix else name
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if relative not in expected_dirs:
                    return False
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    if not walk(child, relative):
                        return False
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if relative not in expected_files or info.st_nlink != 1:
                    return False
                found_files.add(relative)
            else:
                return False
        return True

    # os.listdir(fd) advances the directory position associated with that
    # open file description. Verification can run repeatedly against the same
    # secured descriptor while a Hub snapshot is acquired, so open a fresh
    # descriptor for each inventory walk instead of reusing its cursor.
    try:
        scan_root = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return False
    try:
        return walk(scan_root) and found_files == expected_files
    except OSError:
        return False
    finally:
        os.close(scan_root)


def _report(details: list, artifact: dict, root: str | Path, root_error: str | None,
            inventory_exact: bool | None = None) -> dict:
    states = [item["status"] for item in details]
    present = bool(states) and all(s in ("verified", "present_unverified", "partial", "corrupt") for s in states)
    complete = present and artifact["inventory_complete"] and "partial" not in states
    integrity = complete and all(s == "verified" for s in states)
    blockers = list(artifact.get("release_blockers", []))
    if artifact["availability"] != "verified":
        blockers.append("artifact identity/access/release metadata is unresolved")
    if not artifact["inventory_complete"] or not artifact["required_files"]:
        blockers.append("complete file inventory is unresolved")
    if any("sha256" not in item for item in artifact["required_files"]):
        blockers.append("trusted SHA-256 metadata is missing")
    if artifact.get("strict_tree") and inventory_exact is not True:
        blockers.append("installed file tree does not exactly match the trusted inventory")
        integrity = False
    install_ready = integrity and not blockers
    if root_error or any(s in ("unsafe", "error") for s in states):
        status = "error"
    elif "corrupt" in states:
        status = "corrupt"
    elif states and all(s == "missing" for s in states):
        status = "missing"
    elif any(s in ("missing", "partial") for s in states):
        status = "partial"
    elif install_ready:
        status = "verified"
    else:
        status = "unverified"
    return {"status": status, "root": str(root), "present": present, "complete": complete,
            "integrity_verified": integrity, "inventory_exact": inventory_exact,
            "install_ready": install_ready, "runtime_ready": None,
            "release_blockers": blockers, "error": root_error, "files": details,
            **{f"{state}_files": [item["path"] for item in details if item["status"] == state]
               for state in ("missing", "partial", "corrupt")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check model files against a trusted manifest (offline).")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("role")
    parser.add_argument("platform", help="linux-x86_64, windows-wsl2, or macos-arm64")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = parse_manifest(args.manifest)
        artifact = select_artifact(manifest, args.role, args.platform)
        result = verify_artifact(args.directory, artifact)
    except ManifestError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["install_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
