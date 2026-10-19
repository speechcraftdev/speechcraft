"""FFprobe validation and managed FFmpeg normalization for source audio."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .audio_ingestion import MAX_WAV_BYTES, UploadError


def _tool(name: str) -> str:
    configured = os.environ.get(f"SPEECHCRAFT_{name.upper()}_BIN", "").strip()
    managed = os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1"
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or (managed and (path.is_symlink() or not os.access(path, os.X_OK))):
            raise FileNotFoundError(f"Configured {name} executable is missing or unsafe")
        return str(path.resolve())
    if managed:
        raise FileNotFoundError(f"Managed runtime has no verified {name} executable configured")
    return shutil.which(name) or name


def inspect_and_normalize(source: Path, working_wav: Path) -> dict[str, Any]:
    try:
        probe = subprocess.run(
            [_tool("ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,duration:format=duration",
             "-of", "json", str(source)], capture_output=True, text=True, timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise UploadError("Audio inspection timed out") from exc
    if probe.returncode:
        raise UploadError("The file is not a readable audio recording")
    try:
        probe_data = json.loads(probe.stdout)
        streams = probe_data.get("streams", [])
        stream = streams[0]
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        duration = float(stream.get("duration") or probe_data.get("format", {}).get("duration") or 0)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise UploadError("No valid audio stream was found") from exc
    if sample_rate < 8000 or sample_rate > 192000 or channels < 1 or channels > 8 or duration <= 0:
        raise UploadError("Audio must be 8–192 kHz, 1–8 channels, and have a known duration")
    predicted = int(duration * sample_rate * channels * 2) + 128
    if predicted > MAX_WAV_BYTES:
        raise UploadError("Decoded audio exceeds the supported WAV size limit (4 GiB)")
    compatible_wav = source.suffix.lower() == ".wav" and stream.get("codec_name") == "pcm_s16le"
    if compatible_wav:
        return {"codec": stream["codec_name"], "sample_rate": sample_rate, "channels": channels,
                "duration_seconds": duration, "converted": False}
    working_wav.parent.mkdir(parents=True, exist_ok=True)
    working_wav.unlink(missing_ok=True)
    if shutil.disk_usage(working_wav.parent).free < predicted + 64 * 1024 * 1024:
        raise UploadError("Not enough free disk space for the decoded WAV")
    try:
        result = subprocess.run(
            [_tool("ffmpeg"), "-nostdin", "-v", "error", "-threads", "2", "-i", str(source),
             "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", "-f", "wav", str(working_wav)],
            capture_output=True, text=True, timeout=max(300, min(7200, int(duration * 3))), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        working_wav.unlink(missing_ok=True)
        raise UploadError("Audio conversion timed out") from exc
    if result.returncode:
        working_wav.unlink(missing_ok=True)
        error = result.stderr[-500:] or "FFmpeg error"
        if any(phrase in error.lower() for phrase in ("no space left", "disk full", "not enough space")):
            raise OSError("Insufficient disk space while writing the normalized WAV")
        raise UploadError("Audio conversion failed: " + error)
    if working_wav.stat().st_size > MAX_WAV_BYTES:
        working_wav.unlink(missing_ok=True)
        raise UploadError("Decoded audio exceeds the supported WAV size limit (4 GiB)")
    return {"codec": stream["codec_name"], "sample_rate": sample_rate, "channels": channels,
            "duration_seconds": duration, "converted": True}


def validate_decoded_sample_count(actual_frames: int, sample_rate: int, expected_duration: float) -> None:
    expected_frames = round(expected_duration * sample_rate)
    tolerance_frames = max(
        round(sample_rate * 0.25),
        math.ceil(expected_frames * 0.0001),
    )
    if abs(actual_frames - expected_frames) > tolerance_frames:
        raise UploadError("Decoded WAV sample count does not match the inspected audio stream")
