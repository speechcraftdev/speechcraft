"""Consent-gated ASR model ensure jobs with honest progress telemetry.

Preflight never downloads (local_files_only). This module starts an explicit
worker-side ensure after the user consents in /login3. Progress fields that are
unknown stay null; the UI must not invent percent, speed, or ETA.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .dataset_worker_client import dataset_worker_python, dataset_worker_root
from .defaults import resolve_whisper_model

WhisperSize = Literal["large-v3", "base"]
ALLOWED_MODELS: frozenset[str] = frozenset({"large-v3", "base"})

EnsurePhase = Literal[
    "queued",
    "downloading",
    "verifying",
    "complete",
    "failed",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AsrEnsureJob:
    id: str
    model: str
    repo_id: str
    package_type: str = "faster-whisper"
    phase: EnsurePhase = "queued"
    ok: bool | None = None
    error: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    progress: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)

    def view(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "repo_id": self.repo_id,
            "package_type": self.package_type,
            "phase": self.phase,
            "ok": self.ok,
            "error": self.error,
            "reason_codes": list(self.reason_codes),
            "progress": dict(self.progress),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


_lock = threading.Lock()
_jobs: dict[str, AsrEnsureJob] = {}
_active_by_model: dict[str, str] = {}


def _repo_id_for(model: str) -> str:
    # Keep in sync with workers/dataset FASTER_WHISPER_REPOS without importing the worker.
    mapping = {
        "large-v3": "Systran/faster-whisper-large-v3",
        "base": "Systran/faster-whisper-base",
    }
    return mapping.get(model, model)


def _apply_progress(job: AsrEnsureJob, payload: dict[str, Any]) -> None:
    phase = payload.get("phase")
    if phase in {"queued", "downloading", "verifying", "complete", "failed"}:
        job.phase = phase  # type: ignore[assignment]
    progress = {
        "phase": job.phase,
        "role": "asr",
        "model_name": payload.get("model_name") or job.model,
        "repo_id": payload.get("repo_id") or job.repo_id,
        "package_type": payload.get("package_type") or job.package_type,
        "current_filename": payload.get("current_filename"),
        "downloaded_bytes": payload.get("downloaded_bytes"),
        "total_bytes": payload.get("total_bytes"),
        "bytes_per_second": payload.get("bytes_per_second"),
        "eta_seconds": payload.get("eta_seconds"),
        "files_completed": payload.get("files_completed"),
        "files_total": payload.get("files_total"),
        "transfer_indeterminate": bool(
            payload.get("transfer_indeterminate", True)
            if job.phase == "downloading"
            else payload.get("transfer_indeterminate", False)
        ),
        "indeterminate": job.phase in {"queued", "downloading", "verifying"}
        and payload.get("downloaded_bytes") is None,
        "stalled": payload.get("stalled"),
        "sampled_at": _utc_now(),
        "message": payload.get("message"),
    }
    job.progress = progress
    job.updated_at = _utc_now()


def _run_job(job_id: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        model = job.model
    try:
        if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1":
            with _lock:
                job = _jobs[job_id]
                job.phase = "failed"
                job.ok = False
                job.error = (
                    "Managed SpeechCraft runtime disables ad-hoc model downloads; open /setup to install models."
                )
                job.reason_codes = ["managed_runtime_use_setup"]
                _apply_progress(
                    job,
                    {
                        "phase": "failed",
                        "model_name": model,
                        "message": job.error,
                        "transfer_indeterminate": False,
                    },
                )
            return

        worker_python = dataset_worker_python()
        script = dataset_worker_root() / "scripts" / "ensure_asr.py"
        if not worker_python.exists() or not script.exists():
            raise RuntimeError("Dataset worker ensure_asr script or python is unavailable")

        command = [
            str(worker_python),
            str(script),
            "--model",
            resolve_whisper_model(model),
            "--timeout-seconds",
            "0",
        ]
        with _lock:
            job = _jobs[job_id]
            job.phase = "downloading"
            _apply_progress(
                job,
                {
                    "phase": "downloading",
                    "model_name": model,
                    "repo_id": job.repo_id,
                    "package_type": "faster-whisper",
                    "transfer_indeterminate": True,
                    "message": f"Downloading {model}",
                },
            )

        process = subprocess.Popen(
            command,
            cwd=str(dataset_worker_root()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        final: dict[str, Any] | None = None
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            event = payload.get("event")
            with _lock:
                job = _jobs[job_id]
                if event == "progress":
                    _apply_progress(job, payload)
                elif event == "result":
                    final = payload
                    if payload.get("ok"):
                        job.phase = "complete"
                        job.ok = True
                        job.error = None
                        _apply_progress(
                            job,
                            {
                                "phase": "complete",
                                "model_name": model,
                                "repo_id": payload.get("repo_id") or job.repo_id,
                                "transfer_indeterminate": False,
                                "indeterminate": False,
                                "message": f"{model} ready",
                            },
                        )
                    else:
                        job.phase = "failed"
                        job.ok = False
                        job.error = str(payload.get("error") or "ASR model download failed")
                        codes = payload.get("reason_codes")
                        job.reason_codes = [str(c) for c in codes] if isinstance(codes, list) else []
                        _apply_progress(
                            job,
                            {
                                "phase": "failed",
                                "model_name": model,
                                "message": job.error,
                                "transfer_indeterminate": False,
                            },
                        )
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read()
        returncode = process.wait()
        with _lock:
            job = _jobs[job_id]
            if job.phase not in {"complete", "failed"}:
                job.phase = "failed"
                job.ok = False
                job.error = f"ASR ensure exited without a result (code {returncode})"
                if stderr.strip():
                    job.error = f"{job.error}: {stderr.strip()[-500:]}"
                _apply_progress(
                    job,
                    {
                        "phase": "failed",
                        "model_name": model,
                        "message": job.error,
                        "transfer_indeterminate": False,
                    },
                )
            elif final is None and returncode != 0 and job.ok is not True:
                job.phase = "failed"
                job.ok = False
                job.error = job.error or f"ASR ensure failed (code {returncode})"
    except Exception as exc:  # noqa: BLE001 — surface to the polling UI
        with _lock:
            job = _jobs[job_id]
            job.phase = "failed"
            job.ok = False
            job.error = f"{type(exc).__name__}: {exc}"
            _apply_progress(
                job,
                {
                    "phase": "failed",
                    "model_name": model,
                    "message": job.error,
                    "transfer_indeterminate": False,
                },
            )
    finally:
        with _lock:
            if _active_by_model.get(model) == job_id:
                del _active_by_model[model]


def start_asr_ensure(model: str) -> dict[str, Any]:
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Unsupported ASR model size: {model}")
    if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1":
        raise RuntimeError("managed_runtime_use_setup")

    with _lock:
        existing_id = _active_by_model.get(model)
        if existing_id and existing_id in _jobs:
            existing = _jobs[existing_id]
            if existing.phase in {"queued", "downloading", "verifying"}:
                return existing.view()
        job_id = f"asr-ensure-{uuid.uuid4().hex[:12]}"
        job = AsrEnsureJob(id=job_id, model=model, repo_id=_repo_id_for(model))
        _apply_progress(
            job,
            {
                "phase": "queued",
                "model_name": model,
                "repo_id": job.repo_id,
                "package_type": "faster-whisper",
                "transfer_indeterminate": True,
                "message": f"Queued download for {model}",
            },
        )
        _jobs[job_id] = job
        _active_by_model[model] = job_id

    thread = threading.Thread(target=_run_job, args=(job_id,), name=f"asr-ensure-{model}", daemon=True)
    thread.start()
    with _lock:
        return _jobs[job_id].view()


def get_asr_ensure(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(job_id)
        return None if job is None else job.view()


def reset_asr_ensure_state_for_tests() -> None:
    with _lock:
        _jobs.clear()
        _active_by_model.clear()
