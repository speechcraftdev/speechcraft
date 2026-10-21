#!/usr/bin/env python3
"""Download/cache a faster-whisper ASR model and emit NDJSON progress.

Used by the backend ensure API. Preflight keeps local_files_only=True and never
downloads; this script runs only after explicit user consent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
WORKER_ROOT = SCRIPT_DIR.parent
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure a faster-whisper ASR model is cached locally")
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=0, help="0 disables the alarm timeout")
    args = parser.parse_args()

    if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1":
        emit(
            {
                "event": "result",
                "ok": False,
                "model": args.model,
                "error": "Managed SpeechCraft runtime disables ad-hoc model downloads; use model onboarding.",
                "reason_codes": ["managed_runtime_use_setup"],
            }
        )
        return 1

    from speechcraft_dataset.models import check_asr_model, faster_whisper_repo_id

    repo_id = faster_whisper_repo_id(args.model)
    emit(
        {
            "event": "progress",
            "phase": "downloading",
            "model_name": args.model,
            "repo_id": repo_id,
            "package_type": "faster-whisper",
            "downloaded_bytes": None,
            "total_bytes": None,
            "bytes_per_second": None,
            "eta_seconds": None,
            "transfer_indeterminate": True,
            "files_completed": None,
            "files_total": None,
        }
    )

    def on_progress(update: dict[str, Any]) -> None:
        emit(
            {
                "event": "progress",
                "package_type": "faster-whisper",
                **update,
            }
        )

    result = check_asr_model(
        model=args.model,
        model_path=args.model_path,
        cache_dir=args.cache_dir,
        local_only=False,
        load_model=False,
        timeout_seconds=args.timeout_seconds,
        on_progress=on_progress,
    )
    emit(
        {
            "event": "result",
            "package_type": "faster-whisper",
            "repo_id": result.get("repo_id") or repo_id,
            **result,
        }
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
