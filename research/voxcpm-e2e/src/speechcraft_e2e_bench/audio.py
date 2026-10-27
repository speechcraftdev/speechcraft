from __future__ import annotations
import hashlib, json, subprocess, tempfile
from pathlib import Path
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

TARGET_SR = 16000
def load_audio(path: str | Path, target_sr: int = TARGET_SR) -> tuple[np.ndarray, int]:
    p = Path(path)
    try:
        signal, sr = sf.read(str(p), dtype="float32", always_2d=True)
    except Exception:
        with tempfile.TemporaryDirectory(prefix="speechcraft-decode-") as td:
            wav = Path(td) / "decoded.wav"
            proc = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(p), "-vn", "-f", "wav", str(wav)], capture_output=True, text=True)
            if proc.returncode: raise ValueError(f"Audio decode failed for {p}: {proc.stderr.strip()}")
            signal, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    if signal.size == 0 or not np.isfinite(signal).all(): raise ValueError(f"Empty or non-finite audio: {p}")
    mono = signal.mean(axis=1)
    if sr != target_sr:
        from math import gcd
        g = gcd(sr, target_sr)
        mono = resample_poly(mono, target_sr // g, sr // g).astype(np.float32)
        sr = target_sr
    return mono, sr

def audio_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()

def cache_key(path: str | Path, evaluator: str, version: str, settings: dict) -> str:
    data = json.dumps({"audio":audio_hash(path),"evaluator":evaluator,"version":version,"settings":settings}, sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()
