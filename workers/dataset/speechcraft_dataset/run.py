from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .clip_lab_coordination import assemble_candidate_review_clips_locked
from .audio import create_analysis_audio_variants, inspect_wav
from .buffers import run_processing_buffers
from .diarization import run_diarization
from .export import export_native_candidate_clips
from .io import read_json, resolve_under_root, write_json
from .qc_score_stages import run_speaker_purity_stage, run_transcript_qc_stage
from .vad import run_silero_vad
from .vr_slicer import TRUSTED_GEOMETRY_FINGERPRINT, VR_O0_4


PIPELINE_VERSION = "pretraining_rfc_v1"

WORKER_STAGE_ORDER = [
    "source_audio",
    "audio_variants",
    "vad",
    "diarization",
    "buffers",
    "candidate_review_clips",
    "transcript_qc",
    "speaker_purity",
    "native_export",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_config(source_wavs: list[Path], *, single_speaker: bool, target_speaker_label: str) -> dict[str, Any]:
    return {
        "pipeline_version": PIPELINE_VERSION,
        "mode": "single_speaker" if single_speaker else "diarization",
        "source_wavs": [str(path.resolve()) for path in source_wavs],
        "target_speaker_label": target_speaker_label,
        "analysis_sample_rate": VR_O0_4.sample_rate_hz,
        "vad_backend": "silero",
        "vad_threshold": 0.5,
        "vad_min_speech_ms": 250,
        "vad_min_silence_ms": 250,
        "vad_speech_pad_ms": 80,
        "diarization_window_sec": 900.0,
        "diarization_window_overlap_sec": 30.0,
        "diarization_max_speakers": 6,
        "diarization_batch_size": 16,
        "diarization_speaker_model": "titanet_large",
        "diarization_save_embeddings": False,
        "speaker_sample_count": 3,
        "speaker_sample_duration_sec": 6.0,
        "slicer": "VR",
        "slicer_geometry": VR_O0_4.name,
        "geometry_fingerprint": TRUSTED_GEOMETRY_FINGERPRINT,
        "candidate_min_clip_sec": VR_O0_4.min_clip_sec,
        "candidate_target_clip_sec": VR_O0_4.target_clip_sec,
        "candidate_max_clip_sec": VR_O0_4.max_clip_sec,
        "faster_whisper_model": "large-v3",
        "faster_whisper_beam_size": 5,
        "asr_model_load_timeout_sec": 180,
        "asr_transcribe_timeout_sec": 600,
        "asr_language": "en",
        "asr_task": "transcribe",
        "asr_vad_filter": False,
        "asr_condition_on_previous_text": False,
        "asr_word_timestamps": True,
    }


def load_config(config_path: Path | None, source_wavs: list[Path], *, single_speaker: bool, target_speaker_label: str) -> dict[str, Any]:
    config = default_config(source_wavs, single_speaker=single_speaker, target_speaker_label=target_speaker_label)
    if config_path is not None:
        overrides = read_json(config_path)
        config.update(overrides)
    if not config.get("source_wavs"):
        raise ValueError("At least one source WAV is required")
    config["slicer"] = "VR"
    config["slicer_geometry"] = VR_O0_4.name
    config["geometry_fingerprint"] = TRUSTED_GEOMETRY_FINGERPRINT
    config["candidate_min_clip_sec"] = VR_O0_4.min_clip_sec
    config["candidate_target_clip_sec"] = VR_O0_4.target_clip_sec
    config["candidate_max_clip_sec"] = VR_O0_4.max_clip_sec
    config["analysis_sample_rate"] = VR_O0_4.sample_rate_hz
    config["config_hash"] = config_hash(config)
    return config


def config_hash(config: dict[str, Any]) -> str:
    payload = {key: value for key, value in config.items() if key != "config_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def log_line(run_root: Path, message: str) -> None:
    log_path = resolve_under_root(run_root, "logs/dataset_worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now_iso()} {message}\n")


def write_status(run_root: Path, payload: dict[str, Any]) -> None:
    write_json(resolve_under_root(run_root, "status.json"), payload)


def runtime_versions() -> dict[str, Any]:
    return {
        "dataset_worker": __version__,
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "platform": platform.platform(),
        },
    }


def run_prepare_sources(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_paths = [Path(raw).expanduser().resolve() for raw in config.get("source_wavs", [])]
    sources = []
    for index, source_path in enumerate(source_paths):
        source = inspect_wav(source_path)
        source["source_audio_id"] = f"source_audio_{index:04d}"
        source["source_recording_id"] = source_path.stem
        sources.append(source)

    total_duration = round(sum(float(source["duration_sec"]) for source in sources), 6)
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "source_count": len(sources),
        "sources": sources,
    }
    summary = {
        "source_count": len(sources),
        "total_duration_sec": total_duration,
        "sample_rates": sorted({source["sample_rate"] for source in sources}),
        "channel_counts": sorted({source["num_channels"] for source in sources}),
    }
    write_json(resolve_under_root(run_root, "artifacts/source_audio_manifest.json"), manifest)
    write_json(resolve_under_root(run_root, "artifacts/source_audio_summary.json"), summary)
    return summary


def run_audio_variants(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    source_manifest = read_json(resolve_under_root(run_root, "artifacts/source_audio_manifest.json"))
    sources = list(source_manifest.get("sources") or [])
    return create_analysis_audio_variants(run_root, sources, int(config.get("analysis_sample_rate") or 16000))


def should_stop(current_stage: str, stop_after: str) -> bool:
    return WORKER_STAGE_ORDER.index(current_stage) >= WORKER_STAGE_ORDER.index(stop_after)


def failure_reason_codes(stage: str, exc: Exception) -> list[str]:
    message = str(exc)
    if stage == "vad" and "Silero VAD dependencies are unavailable" in message:
        return ["missing_silero_vad_dependency"]
    if stage == "audio_variants" and "ffmpeg" in message.lower():
        return ["audio_variant_materialization_failed"]
    if stage == "diarization" and "NeMo diarization dependencies are unavailable" in message:
        return ["missing_nemo_dependency"]
    if stage == "diarization":
        return ["diarization_failed"]
    if stage == "buffers":
        return ["processing_buffer_build_failed"]
    if stage == "candidate_review_clips":
        return ["candidate_review_clip_assembly_failed"]
    if stage == "transcript_qc" and "ASR dependencies are unavailable" in message:
        return ["missing_asr_dependency"]
    if stage == "transcript_qc" and "ASR model load timed out" in message:
        return ["asr_model_load_timeout"]
    if stage == "transcript_qc" and "ASR transcription timed out" in message:
        return ["asr_transcription_timeout"]
    if stage == "transcript_qc" and ("Hub" in message or "snapshot" in message or "model" in message.lower()):
        return ["asr_model_unavailable"]
    if stage == "transcript_qc":
        return ["transcript_qc_failed"]
    if stage == "speaker_purity":
        return ["speaker_purity_failed"]
    if stage == "native_export":
        return ["native_export_failed"]
    return ["dataset_worker_failed"]


def _complete_ok(run_root: Path, status: dict[str, Any], stage: str, summary: dict[str, Any]) -> int:
    status.update(
        {
            "ok": True,
            "stage": stage,
            "summary": summary,
            "completed_at": utc_now_iso(),
        }
    )
    write_status(run_root, status)
    return 0


def run_dataset_worker(args: argparse.Namespace) -> int:
    run_root = Path(args.run_root).expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    status = {
        "ok": None,
        "stage": "starting",
        "reason_codes": [],
        "started_at": utc_now_iso(),
        "completed_at": None,
    }
    write_status(run_root, status)
    try:
        source_wavs = [Path(path) for path in args.source_wav]
        config = load_config(
            Path(args.config).expanduser().resolve() if args.config else None,
            source_wavs,
            single_speaker=args.single_speaker,
            target_speaker_label=args.target_speaker_label,
        )
        write_json(resolve_under_root(run_root, "config.json"), config)
        write_json(resolve_under_root(run_root, "runtime_versions.json"), runtime_versions())
        log_line(run_root, "dataset worker started")
        log_line(run_root, f"mode={config['mode']} source_count={len(config['source_wavs'])} slicer=VR")

        status.update({"stage": "source_audio", "reason_codes": []})
        write_status(run_root, status)
        source_summary = run_prepare_sources(run_root, config)
        log_line(run_root, f"source_audio completed summary={source_summary}")
        if should_stop("source_audio", args.stop_after):
            return _complete_ok(run_root, status, "source_audio", source_summary)

        status.update({"stage": "audio_variants", "summary": source_summary})
        write_status(run_root, status)
        audio_variant_summary = run_audio_variants(run_root, config)
        log_line(run_root, f"audio_variants completed summary={audio_variant_summary}")
        if should_stop("audio_variants", args.stop_after):
            return _complete_ok(run_root, status, "audio_variants", audio_variant_summary)

        status.update({"stage": "vad", "summary": audio_variant_summary})
        write_status(run_root, status)
        vad_backend = str(config.get("vad_backend") or "silero").strip().lower()
        if vad_backend != "silero":
            raise ValueError(f"Unsupported VAD backend: {vad_backend}")
        vad_summary = run_silero_vad(run_root, config)
        log_line(run_root, f"vad completed summary={vad_summary}")
        if should_stop("vad", args.stop_after):
            log_line(run_root, "dataset worker completed VAD")
            return _complete_ok(run_root, status, "vad", vad_summary)

        status.update({"stage": "diarization", "summary": vad_summary})
        write_status(run_root, status)
        diarization_summary = run_diarization(run_root, config)
        log_line(run_root, f"diarization completed summary={diarization_summary}")
        diarization_requires_selection = (
            str(config.get("mode") or "single_speaker") == "diarization"
            and "speaker_selection_required" in list(diarization_summary.get("reason_codes") or [])
            and args.stop_after != "diarization"
        )
        if should_stop("diarization", args.stop_after) or diarization_requires_selection:
            status.update(
                {
                    "ok": True,
                    "stage": "diarization",
                    "summary": diarization_summary,
                    "reason_codes": list(diarization_summary.get("reason_codes") or []),
                    "completed_at": utc_now_iso(),
                }
            )
            write_status(run_root, status)
            log_line(run_root, "dataset worker completed diarization")
            return 0

        status.update({"stage": "buffers", "summary": diarization_summary})
        write_status(run_root, status)
        buffer_summary = run_processing_buffers(run_root, config)
        log_line(run_root, f"buffers completed summary={buffer_summary}")
        if should_stop("buffers", args.stop_after):
            log_line(run_root, "dataset worker completed processing buffers")
            return _complete_ok(run_root, status, "buffers", buffer_summary)

        status.update({"stage": "candidate_review_clips", "summary": buffer_summary})
        write_status(run_root, status)
        candidate_review_summary = assemble_candidate_review_clips_locked(run_root, config)
        log_line(run_root, f"candidate_review_clips completed summary={candidate_review_summary}")
        if should_stop("candidate_review_clips", args.stop_after):
            log_line(run_root, "dataset worker completed candidate review clips")
            return _complete_ok(run_root, status, "candidate_review_clips", candidate_review_summary)

        status.update({"stage": "transcript_qc", "summary": candidate_review_summary})
        write_status(run_root, status)
        transcript_qc_summary = run_transcript_qc_stage(run_root, config)
        log_line(run_root, f"transcript_qc completed summary={transcript_qc_summary}")
        if should_stop("transcript_qc", args.stop_after):
            log_line(run_root, "dataset worker completed transcript QC")
            return _complete_ok(run_root, status, "transcript_qc", transcript_qc_summary)

        status.update({"stage": "speaker_purity", "summary": transcript_qc_summary})
        write_status(run_root, status)
        speaker_purity_summary = run_speaker_purity_stage(run_root, config)
        log_line(run_root, f"speaker_purity completed summary={speaker_purity_summary}")
        if should_stop("speaker_purity", args.stop_after):
            log_line(run_root, "dataset worker completed speaker purity")
            return _complete_ok(run_root, status, "speaker_purity", speaker_purity_summary)

        status.update({"stage": "native_export", "summary": speaker_purity_summary})
        write_status(run_root, status)
        native_export_summary = export_native_candidate_clips(run_root, config)
        log_line(run_root, f"native_export completed summary={native_export_summary}")
        log_line(run_root, "dataset worker completed native export")
        return _complete_ok(run_root, status, "native_export", native_export_summary)
    except Exception as exc:
        status.update(
            {
                "ok": False,
                "stage": status.get("stage") or "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "reason_codes": failure_reason_codes(str(status.get("stage") or "failed"), exc),
                "completed_at": utc_now_iso(),
            }
        )
        write_status(run_root, status)
        log_line(run_root, f"dataset worker failed: {status['error']}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SpeechCraft dataset worker")
    parser.add_argument("--run-root", required=True, help="Run root where status, logs, and artifacts are written")
    parser.add_argument("--source-wav", action="append", default=[], help="Source WAV. Repeat for multi-WAV runs.")
    parser.add_argument("--config", default=None, help="Optional JSON config override")
    parser.add_argument("--single-speaker", action="store_true", help="Skip diarization stages in later pipeline steps")
    parser.add_argument("--target-speaker-label", default="speaker_0")
    parser.add_argument(
        "--stop-after",
        choices=WORKER_STAGE_ORDER,
        default="candidate_review_clips",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_dataset_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
