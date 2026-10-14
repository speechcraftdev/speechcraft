"""Offline validation for the model catalog used by setup tooling.

This module intentionally uses only the Python standard library. It checks file
presence and declared integrity; it does not load models or access the network.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

ROLES = {"diarization", "asr", "speech_detection", "speaker_purity"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    pass


def parse_manifest(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, dict):
        value = source
    else:
        try:
            value = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot read manifest: {exc}") from exc
    validate_manifest(value)
    return value


def validate_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")
    models = manifest.get("models")
    if not isinstance(models, list):
        raise ManifestError("models must be a list")
    roles = [model.get("role") for model in models if isinstance(model, dict)]
    if set(roles) != ROLES or len(roles) != len(ROLES):
        raise ManifestError("models must define each required role exactly once")
    for model in models:
        if not isinstance(model, dict):
            raise ManifestError("each model must be an object")
        for key in ("id", "name", "license", "access", "runtime_validation"):
            if not model.get(key):
                raise ManifestError(f"{model.get('role', 'model')}: {key} is required")
        artifacts = model.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise ManifestError(f"{model['role']}: artifacts must be a non-empty list")
        platforms: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ManifestError(f"{model['role']}: artifact must be an object")
            platform = artifact.get("platform")
            if not platform or platform in platforms:
                raise ManifestError(f"{model['role']}: platform must be unique and non-empty")
            platforms.add(platform)
            for key in ("format", "revision", "availability"):
                if not artifact.get(key):
                    raise ManifestError(f"{model['role']} {platform}: {key} is required")
            files = artifact["required_files"]
            if not isinstance(files, list) or (not files and artifact["availability"] != "unresolved"):
                raise ManifestError(f"{model['role']} {platform}: verified artifacts need required_files")
            names: set[str] = set()
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    raise ManifestError("each required file needs a relative path")
                path = PurePosixPath(item["path"])
                if path.is_absolute() or ".." in path.parts or not item["path"]:
                    raise ManifestError(f"unsafe required file path: {item['path']!r}")
                if item["path"] in names:
                    raise ManifestError(f"duplicate required file: {item['path']}")
                names.add(item["path"])
                size = item.get("size_bytes")
                if size is not None and (not isinstance(size, int) or size < 0):
                    raise ManifestError(f"invalid size_bytes for {item['path']}")
                minimum = item.get("min_size_bytes")
                if minimum is not None and (not isinstance(minimum, int) or minimum < 0):
                    raise ManifestError(f"invalid min_size_bytes for {item['path']}")
                digest = item.get("sha256")
                if digest is not None and not SHA256_RE.fullmatch(digest):
                    raise ManifestError(f"invalid sha256 for {item['path']}")


def verify_artifact(root: str | Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """Return missing, partial, corrupt, or verified without fetching anything."""
    base = Path(root)
    details: list[dict[str, Any]] = []
    missing: list[str] = []
    partial: list[str] = []
    corrupt: list[str] = []
    for required in artifact["required_files"]:
        rel = required["path"]
        path = base.joinpath(*PurePosixPath(rel).parts)
        if not path.is_file():
            missing.append(rel)
            details.append({"path": rel, "status": "missing"})
            continue
        size = path.stat().st_size
        expected_size = required.get("size_bytes")
        minimum_size = required.get("min_size_bytes")
        if size == 0 or (expected_size is not None and size < expected_size) or (minimum_size is not None and size < minimum_size):
            partial.append(rel)
            details.append({"path": rel, "status": "partial", "size_bytes": size})
            continue
        actual_hash = None
        digest = required.get("sha256")
        if digest:
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            actual_hash = hasher.hexdigest()
            if actual_hash != digest:
                corrupt.append(rel)
                details.append({"path": rel, "status": "corrupt", "sha256": actual_hash})
                continue
        if expected_size is not None and size != expected_size:
            corrupt.append(rel)
            details.append({"path": rel, "status": "corrupt", "size_bytes": size})
            continue
        details.append({"path": rel, "status": "verified" if digest or expected_size is not None else "present_unverified", "size_bytes": size, "sha256": actual_hash})
    if corrupt:
        status = "corrupt"
    elif missing and not partial and len(missing) == len(details):
        status = "missing"
    elif missing or partial:
        status = "partial"
    elif any(item["status"] == "present_unverified" for item in details):
        status = "unverified"
    else:
        status = "verified"
    return {"status": status, "root": str(base), "files": details, "missing_files": missing, "partial_files": partial, "corrupt_files": corrupt}


def main() -> None:
    parser = argparse.ArgumentParser(description="Check model files against a SpeechCraft manifest (offline).")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("role", choices=sorted(ROLES))
    parser.add_argument("platform", help="Artifact platform key from the manifest")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    manifest = parse_manifest(args.manifest)
    model = next(item for item in manifest["models"] if item["role"] == args.role)
    artifact = next((item for item in model["artifacts"] if item["platform"] == args.platform), None)
    if artifact is None:
        raise SystemExit(f"No {args.role} artifact for platform {args.platform!r}")
    print(json.dumps(verify_artifact(args.directory, artifact), indent=2))


if __name__ == "__main__":
    main()
