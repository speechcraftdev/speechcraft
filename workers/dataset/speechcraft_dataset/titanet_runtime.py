"""Local-only sherpa-onnx adapter for the experimental TitaNet-Large artifact."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def create_sherpa_titanet(model_path: Path) -> Any:
    import sherpa_onnx

    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=str(model_path), num_threads=1, debug=False, provider="cpu"
    )
    if not config.validate():
        raise ValueError(f"Invalid local TitaNet sherpa-onnx configuration: {config}")
    return sherpa_onnx.SpeakerEmbeddingExtractor(config)
