from __future__ import annotations

from pathlib import Path
from typing import Any


def run_transcript_qc_stage(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    from .analyze_whisper_b1_transcript_qc import run_transcript_qc

    return run_transcript_qc(run_root, config)


def run_speaker_purity_stage(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    from .eval_speaker_purity import run_speaker_purity

    return run_speaker_purity(run_root, config)


def run_reference_quality_stage(run_root: Path) -> dict[str, Any]:
    try:
        from .reference_quality import run_reference_quality

        return {"ok": True, **run_reference_quality(run_root)}
    except Exception as exc:  # noqa: BLE001 - RQ is supplementary ranking metadata
        return {"ok": False, "warning": f"{type(exc).__name__}: {exc}"}
