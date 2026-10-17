"""Narrow ASR backends for the dataset worker.

Linux and WSL2 use faster-whisper / CTranslate2. macOS Apple Silicon uses
MLX Whisper. Both return the same segment and word objects the transcript
scorer already reads. Missing confidence fields stay missing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


class AsrError(RuntimeError):
    pass


class UnsupportedAsrOption(AsrError):
    pass


@dataclass
class AsrWord:
    word: str
    start: float | None = None
    end: float | None = None
    probability: float | None = None


@dataclass
class AsrSegment:
    text: str
    start: float | None = None
    end: float | None = None
    avg_logprob: float | None = None
    compression_ratio: float | None = None
    no_speech_prob: float | None = None
    words: list[AsrWord] = field(default_factory=list)


@dataclass
class AsrInfo:
    language: str | None
    backend: str
    model_identity: str
    metadata_gaps: list[str] = field(default_factory=list)


@dataclass
class AsrTranscription:
    segments: list[AsrSegment]
    info: AsrInfo

    def as_transcribe_result(self) -> tuple[list[AsrSegment], AsrInfo]:
        return self.segments, self.info


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _check_timing(start: float | None, end: float | None, label: str) -> None:
    if start is None or end is None:
        return
    if end < start:
        raise AsrError(f"{label} end {end} is before start {start}")


def _word_from_mapping(raw: Any) -> AsrWord:
    if not isinstance(raw, dict):
        raise AsrError("ASR word result is not an object")
    text = str(raw.get("word") or raw.get("text") or "")
    start = _optional_float(raw.get("start"))
    end = _optional_float(raw.get("end"))
    _check_timing(start, end, "word")
    probability = raw.get("probability", raw.get("confidence"))
    return AsrWord(word=text, start=start, end=end, probability=_optional_float(probability))


def _segment_from_mapping(raw: Any) -> AsrSegment:
    if not isinstance(raw, dict):
        raise AsrError("ASR segment result is not an object")
    start = _optional_float(raw.get("start"))
    end = _optional_float(raw.get("end"))
    _check_timing(start, end, "segment")
    words = [_word_from_mapping(word) for word in raw.get("words") or []]
    return AsrSegment(
        text=str(raw.get("text") or ""),
        start=start,
        end=end,
        avg_logprob=_optional_float(raw.get("avg_logprob")),
        compression_ratio=_optional_float(raw.get("compression_ratio")),
        no_speech_prob=_optional_float(raw.get("no_speech_prob")),
        words=words,
    )


def _metadata_gaps(segments: list[AsrSegment]) -> list[str]:
    gaps: list[str] = []
    if not segments or any(segment.no_speech_prob is None for segment in segments):
        gaps.append("no_speech_prob")
    if any(word.probability is None for segment in segments for word in segment.words):
        gaps.append("word_probability")
    return gaps


def normalize_mlx_payload(payload: dict[str, Any], *, model_identity: str) -> AsrTranscription:
    if not isinstance(payload, dict):
        raise AsrError("MLX Whisper returned a non-object result")
    segments = [_segment_from_mapping(row) for row in payload.get("segments") or []]
    language = payload.get("language")
    return AsrTranscription(
        segments=segments,
        info=AsrInfo(
            language=None if language is None else str(language),
            backend="mlx-whisper",
            model_identity=model_identity,
            metadata_gaps=_metadata_gaps(segments),
        ),
    )


def normalize_faster_whisper_result(segments: Any, info: Any, *, model_identity: str) -> AsrTranscription:
    normalized: list[AsrSegment] = []
    for segment in segments:
        words = []
        for word in getattr(segment, "words", None) or []:
            start = getattr(word, "start", None)
            end = getattr(word, "end", None)
            _check_timing(None if start is None else float(start), None if end is None else float(end), "word")
            probability = getattr(word, "probability", None)
            words.append(AsrWord(
                word=str(getattr(word, "word", "") or ""),
                start=None if start is None else float(start),
                end=None if end is None else float(end),
                probability=None if probability is None else float(probability),
            ))
        start = getattr(segment, "start", None)
        end = getattr(segment, "end", None)
        _check_timing(None if start is None else float(start), None if end is None else float(end), "segment")
        no_speech = getattr(segment, "no_speech_prob", None)
        avg_logprob = getattr(segment, "avg_logprob", None)
        compression = getattr(segment, "compression_ratio", None)
        normalized.append(AsrSegment(
            text=str(getattr(segment, "text", "") or ""),
            start=None if start is None else float(start),
            end=None if end is None else float(end),
            avg_logprob=None if avg_logprob is None else float(avg_logprob),
            compression_ratio=None if compression is None else float(compression),
            no_speech_prob=None if no_speech is None else float(no_speech),
            words=words,
        ))
    language = getattr(info, "language", None)
    return AsrTranscription(
        segments=normalized,
        info=AsrInfo(
            language=None if language is None else str(language),
            backend="faster-whisper/ctranslate2",
            model_identity=model_identity,
            metadata_gaps=_metadata_gaps(normalized),
        ),
    )


_MLX_SUPPORTED = {
    "task",
    "language",
    "word_timestamps",
    "condition_on_previous_text",
    "beam_size",
    "vad_filter",
}


def mlx_transcribe_options(options: dict[str, Any]) -> dict[str, Any]:
    """Translate the worker's faster-whisper options into mlx-whisper arguments.

    ``beam_size`` is forwarded to mlx-whisper 0.4.3 as a DecodingOptions
    beam search width. It is not dropped. ``vad_filter=True`` is rejected.
    """
    unknown = sorted(set(options) - _MLX_SUPPORTED)
    if unknown:
        raise UnsupportedAsrOption(f"Unsupported ASR option: {', '.join(unknown)}")
    if options.get("vad_filter") is True:
        raise UnsupportedAsrOption("MLX Whisper does not accept vad_filter; SpeechCraft does not add a VAD here.")
    task = options.get("task", "transcribe")
    if task not in {"transcribe", "translate"}:
        raise UnsupportedAsrOption(f"Unsupported ASR task: {task}")
    translated = {
        "task": task,
        "word_timestamps": bool(options.get("word_timestamps", False)),
        "condition_on_previous_text": bool(options.get("condition_on_previous_text", False)),
        "verbose": False,
        "fp16": True,
    }
    beam_size = options.get("beam_size")
    if beam_size is None or isinstance(beam_size, bool) or int(beam_size) < 1:
        raise UnsupportedAsrOption("MLX Whisper requires an explicit positive beam_size; SpeechCraft does not silently switch to greedy decoding.")
    # mlx-whisper 0.4.3 forwards unknown keywords to DecodingOptions, which accepts beam_size.
    translated["beam_size"] = int(beam_size)
    if options.get("language"):
        translated["language"] = options["language"]
    return translated


class CTranslate2Engine:
    backend = "faster-whisper/ctranslate2"

    def __init__(self, model: Any, model_identity: str) -> None:
        self._model = model
        self.model_identity = model_identity

    def transcribe(self, audio: str, **options: Any) -> tuple[Iterator[AsrSegment], AsrInfo]:
        if options.get("vad_filter") is True:
            raise UnsupportedAsrOption("This SpeechCraft transcription path does not enable a Whisper VAD.")
        segments, info = self._model.transcribe(audio, **options)
        normalized = normalize_faster_whisper_result(segments, info, model_identity=self.model_identity)
        return iter(normalized.segments), normalized.info

    def close(self) -> None:
        model = self._model
        self._model = None
        if model is not None:
            del model


class MlxEngine:
    backend = "mlx-whisper"

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self.model_identity = f"mlx-whisper:{model_dir.name}"

    def transcribe(self, audio: str, **options: Any) -> tuple[list[AsrSegment], AsrInfo]:
        translated = mlx_transcribe_options(options)
        import mlx_whisper

        payload = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=str(self.model_dir),
            **translated,
        )
        normalized = normalize_mlx_payload(payload, model_identity=self.model_identity)
        return normalized.segments, normalized.info

    def close(self) -> None:
        return None


def expected_backend_for_platform(platform_name: str) -> str:
    mapping = {
        "linux-x86_64": "ctranslate2",
        "windows-wsl2": "ctranslate2",
        "macos-arm64": "mlx",
    }
    if platform_name not in mapping:
        raise AsrError(f"No SpeechCraft ASR backend is configured for platform {platform_name or 'unset'}.")
    return mapping[platform_name]


def resolve_managed_backend(model_dir: Path) -> str:
    """Production selection comes from the runtime platform, not from whichever file is present."""
    platform_name = os.environ.get("SPEECHCRAFT_MODEL_PLATFORM", "").strip()
    expected = expected_backend_for_platform(platform_name)
    requested = os.environ.get("SPEECHCRAFT_ASR_BACKEND", "").strip()
    if requested and requested != expected:
        raise AsrError(f"SPEECHCRAFT_ASR_BACKEND={requested} does not match the {platform_name} runtime, which requires {expected}.")
    has_mlx = (model_dir / "weights.npz").is_file() and (model_dir / "config.json").is_file()
    has_ct2 = (model_dir / "model.bin").is_file()
    if expected == "mlx":
        if has_ct2:
            raise AsrError("Refusing to load CTranslate2 weights for the macOS MLX runtime.")
        if not has_mlx:
            raise AsrError("The macOS MLX snapshot is missing weights.npz or config.json.")
        return "mlx"
    if has_mlx and not has_ct2:
        raise AsrError("Refusing to load MLX weights for the Linux CTranslate2 runtime.")
    if not has_ct2:
        raise AsrError("The CTranslate2 snapshot is missing model.bin.")
    return "ctranslate2"


def select_asr_backend(model_dir: Path) -> str:
    if os.environ.get("SPEECHCRAFT_MANAGED_RUNTIME") == "1":
        return resolve_managed_backend(model_dir)
    requested = os.environ.get("SPEECHCRAFT_ASR_BACKEND", "").strip()
    if requested and requested not in {"mlx", "ctranslate2"}:
        raise AsrError(f"Unsupported SPEECHCRAFT_ASR_BACKEND: {requested}")
    has_mlx = (model_dir / "weights.npz").is_file()
    has_ct2 = (model_dir / "model.bin").is_file()
    if requested == "mlx":
        if not has_mlx or has_ct2:
            raise AsrError("MLX Whisper was requested, but the snapshot is not an MLX-only weights.npz directory.")
        return "mlx"
    if requested == "ctranslate2":
        if not has_ct2:
            raise AsrError("CTranslate2 Whisper was requested, but model.bin is not in the local snapshot.")
        return "ctranslate2"
    if has_mlx and not has_ct2:
        return "mlx"
    if has_ct2 and not has_mlx:
        return "ctranslate2"
    if has_mlx and has_ct2:
        raise AsrError("The model directory contains both MLX and CTranslate2 weights. Set SPEECHCRAFT_ASR_BACKEND explicitly.")
    raise AsrError("The local Whisper snapshot has neither MLX weights.npz nor CTranslate2 model.bin.")


def load_asr_engine(model_reference: str, *, device: str = "cpu", compute_type: str = "int8") -> CTranslate2Engine | MlxEngine:
    model_dir = Path(model_reference)
    if not model_dir.is_dir() or model_dir.is_symlink():
        raise AsrError(f"ASR model path is not a real local directory: {model_dir}")
    backend = select_asr_backend(model_dir)
    if backend == "mlx":
        if not (model_dir / "config.json").is_file():
            raise AsrError("MLX Whisper snapshot is missing config.json.")
        return MlxEngine(model_dir.resolve())
    from faster_whisper import WhisperModel

    model = WhisperModel(str(model_dir.resolve()), device=device, compute_type=compute_type, local_files_only=True)
    return CTranslate2Engine(model, model_identity=f"faster-whisper:{model_dir.name}")
