from __future__ import annotations

from typing import Any, Literal

WhisperModelSize = Literal["large-v3", "base"]

LANGUAGE_OPTIONS: dict[str, str] = {
    "auto": "Auto-detect (Whisper)",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
}

WHISPER_MODEL_BY_SIZE: dict[str, str] = {
    "large-v3": "large-v3",
    "base": "base",
}

DATASET_PROCESSING_DEFAULTS: dict[str, Any] = {
    "faster_whisper_beam_size": 5,
    "asr_model_load_timeout_sec": 60,
    "asr_transcribe_timeout_sec": 300,
    "asr_task": "transcribe",
    "asr_vad_filter": False,
    "asr_condition_on_previous_text": False,
    "asr_word_timestamps": True,
}

DATASET_SLICER_HARDCODED: dict[str, Any] = {
    "slicer": "VR",
    "slicer_geometry": "O0_4",
    "geometry_fingerprint": "0ecfe5c7b67aeec535d6cc7435e8e43f3269487d88bb0cefc69891bb86e186d7",
    "candidate_min_clip_sec": 3.0,
    "candidate_target_clip_sec": 8.0,
    "candidate_max_clip_sec": 15.0,
}

DATASET_SLICER_DEFAULTS: dict[str, Any] = {
    **DATASET_SLICER_HARDCODED,
}

# Leftover Slicer UI keys are accepted so the UI does not 400, but they do not
# retune locked O0_4 geometry or packer bounds.
SLICER_UI_CONFIG_KEYS = frozenset(
    {
        "candidate_min_clip_sec",
        "candidate_target_clip_sec",
        "candidate_max_clip_sec",
        "cutpoint_min_gap_ms",
        "cutpoint_left_word_edge_guard_ms",
        "cutpoint_right_word_edge_guard_ms",
    }
)


def resolve_asr_device_and_compute_type() -> tuple[str, str]:
    try:
        import torch
    except ImportError:
        return "cpu", "int8"

    if torch.cuda.is_available():
        return "cuda", "float16"
    return "cpu", "int8"


def resolve_whisper_model(model_size: str) -> str:
    return WHISPER_MODEL_BY_SIZE.get(model_size, WHISPER_MODEL_BY_SIZE["large-v3"])


def resolve_asr_language(language: str) -> str | None:
    normalized = (language or "auto").strip().lower()
    if normalized in {"", "auto"}:
        return None
    return normalized


def build_slicer_config_overrides(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DATASET_SLICER_DEFAULTS)
    if overrides:
        for key, value in overrides.items():
            if key in SLICER_UI_CONFIG_KEYS and key not in DATASET_SLICER_HARDCODED:
                config[key] = value
    return config


def build_dataset_worker_config(
    *,
    language: str = "auto",
    whisper_model_size: WhisperModelSize = "large-v3",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    asr_device, asr_compute_type = resolve_asr_device_and_compute_type()
    asr_language = resolve_asr_language(language)

    config: dict[str, Any] = {
        **DATASET_PROCESSING_DEFAULTS,
        **DATASET_SLICER_DEFAULTS,
        "faster_whisper_model": resolve_whisper_model(whisper_model_size),
        "faster_whisper_device": asr_device,
        "faster_whisper_compute_type": asr_compute_type,
    }
    if asr_language is not None:
        config["asr_language"] = asr_language
    else:
        config["asr_language"] = "auto"

    if overrides:
        config.update(overrides)
        config.update(DATASET_SLICER_HARDCODED)
    return config
