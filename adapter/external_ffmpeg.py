"""Parse FFmpeg silencedetect stderr. No shell-string command construction."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from adapter.external_baselines import FFMPEG_BINARY, FFMPEG_PARAMS

_START = re.compile(
    r"silence_start:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)
_END = re.compile(
    r"silence_end:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
)


class FFmpegSilenceError(RuntimeError):
    """Malformed or missing silencedetect output."""


def ffmpeg_version(binary: str = FFMPEG_BINARY) -> str:
    path = Path(binary)
    if not path.is_file():
        raise FFmpegSilenceError(f"FFmpeg executable missing: {binary}")
    try:
        completed = subprocess.run(
            [binary, "-version"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FFmpegSilenceError(f"could not invoke FFmpeg at {binary}: {exc}") from exc
    if completed.returncode != 0:
        raise FFmpegSilenceError(
            f"ffmpeg -version failed rc={completed.returncode}: {completed.stderr[:400]}"
        )
    first = (completed.stdout or completed.stderr).splitlines()
    if not first:
        raise FFmpegSilenceError("ffmpeg -version produced no output")
    return first[0].strip()


def parse_silencedetect_stderr(
    text: str,
    *,
    duration_sec: float,
) -> list[tuple[float, float]]:
    """Deterministic silence intervals from silencedetect logs.

    A trailing ``silence_start`` without ``silence_end`` is closed at
    ``duration_sec`` (FFmpeg EOF behavior). Any other unpaired event fails.
    """
    if duration_sec < 0:
        raise FFmpegSilenceError(f"invalid duration_sec {duration_sec}")
    intervals: list[tuple[float, float]] = []
    pending: float | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if "silence_start:" in line:
            match = _START.search(line)
            if match is None:
                raise FFmpegSilenceError(f"malformed silencedetect silence_start: {raw_line!r}")
            if pending is not None:
                raise FFmpegSilenceError(
                    f"silencedetect silence_start while previous interval open: {raw_line!r}"
                )
            pending = float(match.group(1))
            continue
        if "silence_end:" in line:
            match = _END.search(line)
            if match is None:
                raise FFmpegSilenceError(f"malformed silencedetect silence_end: {raw_line!r}")
            if pending is None:
                raise FFmpegSilenceError(
                    f"silencedetect silence_end without silence_start: {raw_line!r}"
                )
            end = float(match.group(1))
            start = pending
            pending = None
            if end < start:
                raise FFmpegSilenceError(
                    f"silencedetect inverted interval [{start}, {end}]"
                )
            intervals.append((start, end))
    if pending is not None:
        end = float(duration_sec)
        if end < pending:
            raise FFmpegSilenceError(
                f"trailing silence_start {pending} beyond duration {duration_sec}"
            )
        intervals.append((pending, end))
    return intervals


def silencedetect_command(
    wav_path: Path,
    *,
    binary: str = FFMPEG_BINARY,
    filter_arg: str | None = None,
) -> list[str]:
    filt = filter_arg if filter_arg is not None else str(FFMPEG_PARAMS["filter"])
    return [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(wav_path),
        "-af",
        filt,
        "-f",
        "null",
        "-",
    ]


def run_silencedetect_on_wav(
    wav_path: Path,
    *,
    duration_sec: float,
    binary: str = FFMPEG_BINARY,
    filter_arg: str | None = None,
) -> tuple[list[tuple[float, float]], str]:
    if not Path(binary).is_file():
        raise FFmpegSilenceError(f"FFmpeg executable missing: {binary}")
    command = silencedetect_command(wav_path, binary=binary, filter_arg=filter_arg)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FFmpegSilenceError(f"FFmpeg invocation failed: {exc}") from exc
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        raise FFmpegSilenceError(
            f"FFmpeg silencedetect failed rc={completed.returncode}: {stderr[-800:]}"
        )
    intervals = parse_silencedetect_stderr(stderr, duration_sec=duration_sec)
    return intervals, stderr
