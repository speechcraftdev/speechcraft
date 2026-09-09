from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .io import read_json, read_jsonl, resolve_under_root, run_command, sha256_file, write_json, write_jsonl
from .vad import run_silero_vad

COMMUNITY1_BACKEND = "pyannote_community_1"
COMMUNITY1_EMBED_MIN_BYTES = 26646242
COMMUNITY1_SEG_MIN_BYTES = 5906507
_SPEAKER_NUMERIC = re.compile(r"(\d+)$")


def sec_to_sample(seconds: float, sample_rate: int) -> int:
    return int(round(seconds * sample_rate))


def sample_to_sec(sample_index: int, sample_rate: int) -> float:
    return round(sample_index / sample_rate, 6)


def extract_audio_window(source_path: Path, output_path: Path, start_sec: float, end_sec: float, sample_rate: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start_sec:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{max(0.0, end_sec - start_sec):.3f}",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )


def speechcraft_speaker_id(label: str) -> str:
    """Map a pyannote speaker label to a stable SpeechCraft speaker id."""
    text = str(label).strip()
    if not text:
        raise ValueError("pyannote speaker label is empty")
    if text.startswith("speaker_"):
        suffix = text[len("speaker_") :]
        if suffix.isdigit():
            return f"speaker_{int(suffix)}"
    match = _SPEAKER_NUMERIC.search(text)
    if match is None:
        raise ValueError(f"unrecognized pyannote speaker label: {label!r}")
    return f"speaker_{int(match.group(1))}"


def iter_pyannote_turns(annotation: Any):
    if hasattr(annotation, "itertracks"):
        yield from annotation.itertracks(yield_label=True)
        return
    for item in annotation:
        if len(item) == 3:
            yield item
        else:
            turn, speaker = item
            yield turn, None, speaker


def community1_annotation(output: Any) -> Any:
    return getattr(output, "speaker_diarization", None) or output


def annotation_to_speechcraft_regions(
    annotation: Any,
    *,
    source_audio_id: str,
    analysis_audio_path: str,
    sample_rate: int,
    backend_version: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for turn, _, speaker in iter_pyannote_turns(annotation):
        start_sec = float(turn.start)
        end_sec = float(turn.end)
        if end_sec <= start_sec:
            continue
        speaker_id = speechcraft_speaker_id(str(speaker))
        start_sample = sec_to_sample(start_sec, sample_rate)
        end_sample = sec_to_sample(end_sec, sample_rate)
        if end_sample <= start_sample:
            continue
        rows.append(
            {
                "id": f"{speaker_id}-{source_audio_id}-{start_sample}-{end_sample}",
                "source_audio_id": source_audio_id,
                "analysis_audio_path": analysis_audio_path,
                "speaker_id": speaker_id,
                "start_sample": start_sample,
                "end_sample": end_sample,
                "start_sec": sample_to_sec(start_sample, sample_rate),
                "end_sec": sample_to_sec(end_sample, sample_rate),
                "backend": COMMUNITY1_BACKEND,
                "backend_version": backend_version,
                "rfc_compliant": True,
            }
        )
    return rows


def community1_snapshot_ready(path: Path) -> tuple[bool, str]:
    if not path.is_dir():
        return False, f"pyannote Community-1 snapshot is missing or not a directory: {path}"
    embed = path / "embedding" / "pytorch_model.bin"
    seg = path / "segmentation" / "pytorch_model.bin"
    if not embed.is_file() or embed.stat().st_size < COMMUNITY1_EMBED_MIN_BYTES:
        return False, f"pyannote Community-1 snapshot is incomplete: embedding weights missing or too small at {embed}"
    if not seg.is_file() or seg.stat().st_size < COMMUNITY1_SEG_MIN_BYTES:
        return False, f"pyannote Community-1 snapshot is incomplete: segmentation weights missing or too small at {seg}"
    return True, str(path)


def resolve_community1_model_path(config: dict[str, Any]) -> Path:
    raw = str(config.get("diarization_model_path") or os.environ.get("SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH") or "").strip()
    if not raw:
        raise RuntimeError(
            "pyannote Community-1 local model path is missing. "
            "Set diarization_model_path or SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH to a local snapshot directory."
        )
    path = Path(raw).expanduser().resolve()
    ok, reason = community1_snapshot_ready(path)
    if not ok:
        raise RuntimeError(reason)
    return path


def resolve_diarization_device(config: dict[str, Any]) -> str:
    explicit = str(config.get("diarization_device") or "").strip()
    if explicit:
        return explicit
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def load_analysis_waveform(path: Path, expected_sample_rate: int) -> tuple[Any, int]:
    import numpy as np
    import soundfile as sf

    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if int(rate) != int(expected_sample_rate):
        raise ValueError(f"Analysis sample-rate mismatch for {path}: {rate} != {expected_sample_rate}")
    waveform = np.asarray(audio.T, dtype=np.float32)
    return waveform, int(rate)


def concat_waveforms_with_gaps(waveforms: list[Any], sample_rate: int, gap_sec: float) -> Any:
    """Concatenate channel-first waveforms with deterministic silence gaps."""
    import numpy as np

    arrays: list[Any] = []
    for waveform in waveforms:
        array = np.asarray(waveform, dtype=np.float32)
        if array.ndim == 1:
            array = array[None, :]
        if array.ndim != 2:
            raise ValueError("waveforms must be shaped (channel, time)")
        arrays.append(array)
    if not arrays:
        raise ValueError("at least one waveform is required")
    gap_samples = max(0, int(round(float(gap_sec) * sample_rate)))
    channels = int(arrays[0].shape[0])
    parts: list[Any] = []
    for index, array in enumerate(arrays):
        if int(array.shape[0]) != channels:
            raise ValueError("concat waveforms must share a channel count")
        parts.append(array)
        if index < len(arrays) - 1 and gap_samples > 0:
            parts.append(np.zeros((channels, gap_samples), dtype=np.float32))
    return np.concatenate(parts, axis=1)


def pyannote_audio_dict(waveform: Any, sample_rate: int) -> dict[str, Any]:
    import numpy as np
    import torch

    if isinstance(waveform, torch.Tensor):
        tensor = waveform
        if tensor.dim() == 1:
            tensor = tensor.unsqueeze(0)
    else:
        array = np.asarray(waveform, dtype=np.float32)
        if array.ndim == 1:
            array = array[None, :]
        tensor = torch.from_numpy(np.ascontiguousarray(array))
    return {"waveform": tensor, "sample_rate": int(sample_rate)}


def load_community1_pipeline(model_path: Path, device: str) -> Any:
    try:
        import torch
        from pyannote.audio import Pipeline
    except Exception as exc:
        raise RuntimeError(f"pyannote.audio is unavailable: {type(exc).__name__}: {exc}") from exc

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        pipeline = Pipeline.from_pretrained(str(model_path))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to load pyannote Community-1 from {model_path}: {type(exc).__name__}: {exc}"
        ) from exc
    if pipeline is None:
        raise RuntimeError(f"Failed to load pyannote Community-1 from {model_path}: Pipeline.from_pretrained returned None")
    pipeline.to(torch.device(device))
    return pipeline


def run_community1_pipeline(pipeline: Any, waveform: Any, sample_rate: int) -> Any:
    output = pipeline(pyannote_audio_dict(waveform, sample_rate))
    return community1_annotation(output)


def pyannote_audio_version() -> str | None:
    try:
        import pyannote.audio

        return getattr(pyannote.audio, "__version__", None)
    except Exception:
        return None


def build_single_speaker_regions(variants: list[dict[str, Any]], vad_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    variant_paths = {str(variant["source_audio_id"]): str(variant["path"]) for variant in variants}
    rows: list[dict[str, Any]] = []
    for segment in vad_segments:
        source_audio_id = str(segment["source_audio_id"])
        start_sample = int(segment["analysis_start_sample"])
        end_sample = int(segment["analysis_end_sample"])
        rows.append(
            {
                "id": f"speaker_0-{source_audio_id}-{start_sample}-{end_sample}",
                "source_audio_id": source_audio_id,
                "analysis_audio_path": variant_paths[source_audio_id],
                "speaker_id": "speaker_0",
                "start_sample": start_sample,
                "end_sample": end_sample,
                "start_sec": float(segment["analysis_start_sec"]),
                "end_sec": float(segment["analysis_end_sec"]),
                "backend": "single_speaker_vad_passthrough",
                "backend_version": None,
                "vad_source": "silero_external",
                "rfc_compliant": True,
            }
        )
    return rows


def write_speaker_samples(
    run_root: Path,
    rows: list[dict[str, Any]],
    *,
    sample_rate_by_source: dict[str, int],
    analysis_path_by_source: dict[str, str],
    sample_count: int,
    sample_duration_sec: float,
) -> list[dict[str, Any]]:
    by_speaker: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_speaker.setdefault(str(row["speaker_id"]), []).append(row)

    manifest_rows: list[dict[str, Any]] = []
    for speaker_id, speaker_rows in sorted(by_speaker.items()):
        ranked = sorted(speaker_rows, key=lambda row: int(row["end_sample"]) - int(row["start_sample"]), reverse=True)[:sample_count]
        for index, row in enumerate(ranked):
            source_audio_id = str(row["source_audio_id"])
            sample_rate = sample_rate_by_source[source_audio_id]
            duration_samples = min(int(row["end_sample"]) - int(row["start_sample"]), sec_to_sample(sample_duration_sec, sample_rate))
            if duration_samples <= 0:
                continue
            start_sample = int(row["start_sample"])
            end_sample = start_sample + duration_samples
            analysis_path = resolve_under_root(run_root, analysis_path_by_source[source_audio_id])
            relative_audio_path = f"artifacts/speaker_samples/{speaker_id}_{index:02d}.wav"
            output_path = resolve_under_root(run_root, relative_audio_path)
            extract_audio_window(
                analysis_path,
                output_path,
                start_sample / sample_rate,
                end_sample / sample_rate,
                sample_rate,
            )
            manifest_rows.append(
                {
                    "sample_id": f"{speaker_id}_{index:02d}",
                    "speaker_id": speaker_id,
                    "source_audio_id": source_audio_id,
                    "region_id": str(row["id"]),
                    "audio_path": relative_audio_path,
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "duration_sec": sample_to_sec(duration_samples, sample_rate),
                    "content_hash": sha256_file(output_path),
                }
            )
    return manifest_rows


def build_summary(
    rows: list[dict[str, Any]],
    sample_rows: list[dict[str, Any]],
    *,
    stage: str,
    config: dict[str, Any],
    input_hashes: dict[str, str],
    backend: str,
    backend_version: str | None,
    mode: str,
    reason_codes: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    per_speaker: dict[str, dict[str, Any]] = {}
    for row in rows:
        speaker_id = str(row["speaker_id"])
        speaker = per_speaker.setdefault(speaker_id, {"segment_count": 0, "duration_sec": 0.0, "source_audio_ids": set()})
        speaker["segment_count"] += 1
        speaker["duration_sec"] += max(0.0, float(row["end_sec"]) - float(row["start_sec"]))
        speaker["source_audio_ids"].add(str(row["source_audio_id"]))
    for speaker in per_speaker.values():
        speaker["duration_sec"] = round(float(speaker["duration_sec"]), 6)
        speaker["source_audio_ids"] = sorted(speaker["source_audio_ids"])

    summary = {
        "stage": stage,
        "config_hash": str(config.get("config_hash") or ""),
        "input_artifact_hashes": input_hashes,
        "backend": backend,
        "backend_version": backend_version,
        "mode": mode,
        "speaker_count": len(per_speaker),
        "speaker_ids": sorted(per_speaker),
        "speaker_sample_count": len(sample_rows),
        "per_speaker": per_speaker,
        "reason_codes": reason_codes or [],
    }
    if extra:
        summary.update(extra)
    return summary


def plan_concat_layout(variants: list[dict[str, Any]], gap_sec: float) -> list[dict[str, Any]]:
    """Cumulative concat offsets (seconds) for each source variant, separated by
    a silence gap so no speech region spans a file boundary. Pure/testable."""
    layout: list[dict[str, Any]] = []
    cursor = 0.0
    for variant in variants:
        duration = float(variant["analysis_duration_sec"])
        start = cursor
        end = start + duration
        layout.append(
            {
                "source_audio_id": str(variant["source_audio_id"]),
                "path": str(variant["path"]),
                "start_sec": round(start, 6),
                "end_sec": round(end, 6),
                "duration_sec": round(duration, 6),
            }
        )
        cursor = end + gap_sec
    return layout


def remap_concat_regions_to_sources(
    regions: list[dict[str, Any]], layout: list[dict[str, Any]], sample_rate: int
) -> list[dict[str, Any]]:
    """Map concat-time speaker regions back to per-source local coordinates.

    Pieces that fall entirely inside an inter-file silence gap are dropped.
    Regions that cross a source boundary are split onto each overlapping source.
    """
    rows: list[dict[str, Any]] = []
    for region in regions:
        r_start = float(region["start_sec"])
        r_end = float(region["end_sec"])
        speaker_id = str(region["speaker_id"])
        for entry in layout:
            clip_start = max(r_start, float(entry["start_sec"]))
            clip_end = min(r_end, float(entry["end_sec"]))
            if clip_end <= clip_start:
                continue
            offset = float(entry["start_sec"])
            local_start = clip_start - offset
            local_end = clip_end - offset
            start_sample = sec_to_sample(local_start, sample_rate)
            end_sample = sec_to_sample(local_end, sample_rate)
            if end_sample <= start_sample:
                continue
            source_audio_id = str(entry["source_audio_id"])
            rows.append(
                {
                    **region,
                    "speaker_id": speaker_id,
                    "source_audio_id": source_audio_id,
                    "analysis_audio_path": str(entry["path"]),
                    "start_sec": sample_to_sec(start_sample, sample_rate),
                    "end_sec": sample_to_sec(end_sample, sample_rate),
                    "start_sample": start_sample,
                    "end_sample": end_sample,
                    "id": f"{speaker_id}-{source_audio_id}-{start_sample}-{end_sample}",
                }
            )
    return rows


def _write_diarization_artifacts(
    run_root: Path,
    config: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    sample_rate_by_source: dict[str, int],
    analysis_path_by_source: dict[str, str],
    sample_count: int,
    sample_duration_sec: float,
    backend: str,
    backend_version: str | None,
    mode: str,
    extra: dict[str, Any],
    input_hashes: dict[str, str],
    auto_select_speaker_0: bool,
) -> dict[str, Any]:
    speaker_regions_path = resolve_under_root(run_root, "artifacts/speaker_regions.jsonl")
    samples_manifest_path = resolve_under_root(run_root, "artifacts/speaker_samples_manifest.json")
    selection_path = resolve_under_root(run_root, "artifacts/speaker_selection.json")

    sample_rows = write_speaker_samples(
        run_root,
        rows,
        sample_rate_by_source=sample_rate_by_source,
        analysis_path_by_source=analysis_path_by_source,
        sample_count=sample_count,
        sample_duration_sec=sample_duration_sec,
    )
    write_jsonl(speaker_regions_path, rows)
    write_json(samples_manifest_path, sample_rows)

    available_speaker_ids = sorted({str(row["speaker_id"]) for row in rows})
    if auto_select_speaker_0:
        selection: dict[str, Any] = {
            "mode": "single_speaker",
            "selected": True,
            "target_speaker_id": "speaker_0",
            "source": "auto",
            "available_speaker_ids": ["speaker_0"],
            "updated_at": None,
        }
        reason_codes: list[str] = []
    else:
        selection = {
            "mode": "diarization",
            "selected": False,
            "target_speaker_id": None,
            "source": "pending_user_selection",
            "available_speaker_ids": available_speaker_ids,
            "updated_at": None,
        }
        if selection_path.exists():
            existing = read_json(selection_path)
            existing_target = str(existing.get("target_speaker_id") or "").strip()
            if existing_target in available_speaker_ids:
                selection = {
                    "mode": "diarization",
                    "selected": True,
                    "target_speaker_id": existing_target,
                    "source": str(existing.get("source") or "user"),
                    "available_speaker_ids": available_speaker_ids,
                    "updated_at": existing.get("updated_at"),
                }
        reason_codes = [] if selection["selected"] else ["speaker_selection_required"]
    write_json(selection_path, selection)

    summary = build_summary(
        rows,
        sample_rows,
        stage="diarization",
        config=config,
        input_hashes=input_hashes,
        backend=backend,
        backend_version=backend_version,
        mode=mode,
        reason_codes=reason_codes,
        extra={"selection_written": True, **extra},
    )
    summary["output_hashes"] = {
        "speaker_regions_jsonl": sha256_file(speaker_regions_path),
        "speaker_samples_manifest_json": sha256_file(samples_manifest_path),
        "speaker_selection_json": sha256_file(selection_path),
    }
    write_json(resolve_under_root(run_root, "artifacts/speaker_regions_summary.json"), summary)
    return summary


def _run_pyannote_diarization(
    run_root: Path,
    config: dict[str, Any],
    variants: list[dict[str, Any]],
    *,
    sample_rate_by_source: dict[str, int],
    analysis_path_by_source: dict[str, str],
    sample_count: int,
    sample_duration_sec: float,
) -> dict[str, Any]:
    if not variants:
        raise ValueError("diarization requires at least one analysis audio variant")
    model_path = resolve_community1_model_path(config)
    device = resolve_diarization_device(config)
    sample_rate = int(variants[0]["analysis_sample_rate"])
    pipeline = load_community1_pipeline(model_path, device)
    backend_version = pyannote_audio_version()
    waveforms = [
        load_analysis_waveform(resolve_under_root(run_root, str(variant["path"])), sample_rate)[0]
        for variant in variants
    ]
    gap_sec = float(config.get("diarization_concat_gap_sec") or 1.0)
    extra: dict[str, Any] = {
        "device": device,
        "model_path": str(model_path),
        "source_audio_ids": [str(variant["source_audio_id"]) for variant in variants],
        "source_count": len(variants),
    }
    if len(variants) == 1:
        annotation = run_community1_pipeline(pipeline, waveforms[0], sample_rate)
        rows = annotation_to_speechcraft_regions(
            annotation,
            source_audio_id=str(variants[0]["source_audio_id"]),
            analysis_audio_path=str(variants[0]["path"]),
            sample_rate=sample_rate,
            backend_version=backend_version,
        )
        extra.update({"multi_file": False, "source_audio_id": str(variants[0]["source_audio_id"])})
    else:
        layout = plan_concat_layout(variants, gap_sec)
        concat_waveform = concat_waveforms_with_gaps(waveforms, sample_rate, gap_sec)
        annotation = run_community1_pipeline(pipeline, concat_waveform, sample_rate)
        concat_rows = annotation_to_speechcraft_regions(
            annotation,
            source_audio_id="concat",
            analysis_audio_path="concat",
            sample_rate=sample_rate,
            backend_version=backend_version,
        )
        rows = remap_concat_regions_to_sources(concat_rows, layout, sample_rate)
        extra.update({"multi_file": True, "concat_gap_sec": gap_sec})

    audio_variants_manifest_path = resolve_under_root(run_root, "artifacts/audio_variants_manifest.json")
    return _write_diarization_artifacts(
        run_root,
        config,
        rows,
        sample_rate_by_source=sample_rate_by_source,
        analysis_path_by_source=analysis_path_by_source,
        sample_count=sample_count,
        sample_duration_sec=sample_duration_sec,
        backend=COMMUNITY1_BACKEND,
        backend_version=backend_version,
        mode=str(config.get("mode") or "diarization"),
        extra=extra,
        input_hashes={"audio_variants_manifest": sha256_file(audio_variants_manifest_path)},
        auto_select_speaker_0=False,
    )


def run_diarization(run_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    audio_variants_manifest_path = resolve_under_root(run_root, "artifacts/audio_variants_manifest.json")
    variants = list(read_json(audio_variants_manifest_path).get("variants") or [])
    sample_rate_by_source = {str(variant["source_audio_id"]): int(variant["analysis_sample_rate"]) for variant in variants}
    analysis_path_by_source = {str(variant["source_audio_id"]): str(variant["path"]) for variant in variants}
    mode = str(config.get("mode") or "single_speaker")
    sample_count = int(config.get("speaker_sample_count") or 3)
    sample_duration_sec = float(config.get("speaker_sample_duration_sec") or 6.0)

    if mode == "single_speaker":
        run_silero_vad(run_root, config)
        vad_segments_path = resolve_under_root(run_root, "artifacts/vad_segments.jsonl")
        vad_segments = read_jsonl(vad_segments_path)
        rows = build_single_speaker_regions(variants, vad_segments)
        return _write_diarization_artifacts(
            run_root,
            config,
            rows,
            sample_rate_by_source=sample_rate_by_source,
            analysis_path_by_source=analysis_path_by_source,
            sample_count=sample_count,
            sample_duration_sec=sample_duration_sec,
            backend="single_speaker_vad_passthrough",
            backend_version=None,
            mode=mode,
            extra={},
            input_hashes={
                "audio_variants_manifest": sha256_file(audio_variants_manifest_path),
                "vad_segments_jsonl": sha256_file(vad_segments_path),
            },
            auto_select_speaker_0=True,
        )

    return _run_pyannote_diarization(
        run_root,
        config,
        variants,
        sample_rate_by_source=sample_rate_by_source,
        analysis_path_by_source=analysis_path_by_source,
        sample_count=sample_count,
        sample_duration_sec=sample_duration_sec,
    )
