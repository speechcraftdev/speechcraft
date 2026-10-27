from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

@dataclass
class AudioItem:
    id: str
    path: Path
    transcript: str | None = None
    prompt_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    kind: str = "generated"

@dataclass
class Dataset:
    path: Path
    speaker_id: str
    real: list[AudioItem]
    prompts: dict[str, str]
    conditions: dict[str, list[AudioItem]]
    condition_metadata: dict[str, dict[str, Any]]
    attempts: list[dict[str, Any]]
    raw: dict[str, Any]
