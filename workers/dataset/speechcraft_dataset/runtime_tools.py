"""Resolve tools from controller-owned paths in managed processing mode."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def executable(name: str) -> str:
    if name not in {"ffmpeg", "ffprobe"}:
        raise ValueError(f"Unsupported runtime tool: {name}")
    configured = os.environ.get(f"SPEECHCRAFT_{name.upper()}_BIN", "").strip()
    if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1":
        if not configured:
            raise FileNotFoundError(f"Managed runtime has no verified {name} executable configured.")
        target = Path(configured).expanduser()
        if target.is_symlink() or not target.is_file() or not os.access(target, os.X_OK):
            raise FileNotFoundError(f"Configured managed {name} executable is missing or unsafe: {target}")
        return str(target.resolve())
    return configured or shutil.which(name) or name


def verify_managed_model(role: str, artifact_directory: str | Path, platform: str) -> dict:
    if role not in {"speech_detection", "asr", "diarization", "speaker_purity"}:
        raise ValueError(f"Unsupported managed model role: {role}")
    from speechcraft_models.manifest import load_catalog, select_artifact, verify_artifact

    report = verify_artifact(artifact_directory, select_artifact(load_catalog(), role, platform))
    if report.get("status") != "verified" or report.get("integrity_verified") is not True or report.get("install_ready") is not True:
        raise RuntimeError(f"The controller-managed {role} model failed manifest integrity verification: {report.get('status')}")
    return report
