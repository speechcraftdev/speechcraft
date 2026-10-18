"""Offline inference adapter for WeSpeaker ECAPA-TDNN512-LM."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np

MODEL_ID = "wespeaker-ecapa-tdnn512-lm"
MODEL_REPO = "Wespeaker/wespeaker-ecapa-tdnn512-LM"
MODEL_REVISION = "a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b"
MODEL_FILENAME = "voxceleb_ECAPA512_LM.onnx"
MODEL_SIZE_BYTES = 24_861_931
MODEL_SHA256 = "d71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0"
EMBEDDING_DIM = 192
FRONTEND_ID = "wespeaker-kaldi-fbank80-25ms-10ms-hamming-dither0-htk-cmn-v1"
LICENSE = "CC-BY-4.0"
MIN_AUDIO_SEC = 0.5
SAMPLE_RATE = 16000


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wespeaker_features(samples: np.ndarray) -> np.ndarray:
    """Produce the publisher-compatible Kaldi fbank features used by the benchmark."""
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size < int(SAMPLE_RATE * MIN_AUDIO_SEC):
        raise ValueError("WeSpeaker ECAPA requires at least 0.5 seconds of audio at 16 kHz.")
    if not np.isfinite(audio).all():
        raise ValueError("Audio contains non-finite samples.")
    if float(np.max(np.abs(audio), initial=0.0)) <= 1e-8:
        raise ValueError("Audio is silent; no speaker embedding can be inferred.")

    import torch
    from torchaudio.compliance import kaldi

    waveform = torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0) * 32768.0
    features = kaldi.fbank(
        waveform,
        num_mel_bins=80,
        frame_length=25,
        frame_shift=10,
        sample_frequency=SAMPLE_RATE,
        window_type="hamming",
        dither=0.0,
        energy_floor=0.0,
        use_energy=False,
        htk_compat=True,
    ).numpy().astype(np.float32, copy=False)
    features -= features.mean(axis=0, keepdims=True)
    if features.ndim != 2 or features.shape[1] != 80 or not np.isfinite(features).all():
        raise RuntimeError("WeSpeaker fbank extraction returned invalid features.")
    return np.ascontiguousarray(features[None, :, :], dtype=np.float32)


class WeSpeakerEcapa:
    backend = "onnxruntime-cpu"
    calibration_status = "uncalibrated_review_required"
    model_id = MODEL_ID
    model_repo = MODEL_REPO
    model_revision = MODEL_REVISION
    model_sha256 = MODEL_SHA256
    frontend = FRONTEND_ID
    license = LICENSE
    dimension = EMBEDDING_DIM

    def __init__(self, model_path: Path, session: Any | None = None):
        self.model_path = Path(model_path)
        if self.model_path.is_symlink() or not self.model_path.is_file() or self.model_path.name != MODEL_FILENAME:
            raise FileNotFoundError(f"Expected a regular {MODEL_FILENAME} file.")
        if self.model_path.stat().st_size != MODEL_SIZE_BYTES or sha256_file(self.model_path) != MODEL_SHA256:
            raise ValueError("WeSpeaker ECAPA model failed the pinned size or SHA-256 verification.")
        if session is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        if len(inputs) != 1 or inputs[0].name != "feats" or len(outputs) != 1 or outputs[0].name != "embs":
            raise RuntimeError("WeSpeaker ECAPA ONNX interface must be feats -> embs.")
        output_shape = outputs[0].shape
        if not output_shape or output_shape[-1] != EMBEDDING_DIM:
            raise RuntimeError(f"WeSpeaker ECAPA output dimension is not {EMBEDDING_DIM}: {output_shape}.")
        self.session = session

    def infer_segment(self, samples: np.ndarray) -> tuple[np.ndarray, None]:
        features = wespeaker_features(samples)
        raw = self.session.run(["embs"], {"feats": features})[0]
        embedding = np.asarray(raw, dtype=np.float64).reshape(-1)
        if embedding.size != EMBEDDING_DIM or not np.isfinite(embedding).all():
            raise RuntimeError("WeSpeaker ECAPA returned an invalid 192-dimensional embedding.")
        if float(np.linalg.norm(embedding)) <= 1e-12:
            raise RuntimeError("WeSpeaker ECAPA returned a zero-norm embedding.")
        return embedding, None


def load_ecapa_model(model_name: str, device_arg: str, model_path: str | Path | None = None) -> WeSpeakerEcapa:
    if device_arg not in {"auto", "cpu"}:
        raise RuntimeError("Speaker Purity WeSpeaker ECAPA inference currently uses the pinned CPU ONNX Runtime path.")
    if model_name != MODEL_ID:
        raise ValueError(f"Unsupported Speaker Purity model: {model_name}")
    if model_path is None:
        raise RuntimeError("A locally acquired, model-manager-verified WeSpeaker ECAPA file is required; network inference is disabled.")
    return WeSpeakerEcapa(Path(model_path).expanduser())
