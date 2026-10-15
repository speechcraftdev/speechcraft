#!/usr/bin/env python3
"""Offline, machine-readable inference probes for the managed Linux worker."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKER_ROOT = Path(__file__).resolve().parents[1]
if str(WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKER_ROOT))

PROBE_FIXTURES = {
    "ffmpeg": ("speech-compressed.mp3", "e61a459dfc841b060aa2db5119ddd527346177ab965f290986a46c965302c33a", 144908),
    "silero": ("speech-silence.wav", "65afa781f190dd7e6ee3b719e0b200b1224d057d4ae4c5077cd1b125685cda06", 640078),
    "whisper": ("speech.wav", "ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd", 576078),
    "titanet": ("speech.wav", "ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd", 576078),
    "pyannote": ("speech.wav", "ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd", 576078),
    "cuda": ("speech.wav", "ecb6f72edf6207010324b8efc314893eea81688d24392c8ee4ad39074fb420cd", 576078),
}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_file(component: str) -> Path:
    raw = os.environ.get("SPEECHCRAFT_RUNTIME_PROBE_AUDIO", "").strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        name, expected_sha, expected_size = PROBE_FIXTURES[component]
        path = WORKER_ROOT / "runtime-fixtures" / name
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
        raise RuntimeError("The configured probe recording is missing, unsafe, or larger than 100 MiB.")
    if not raw:
        name, expected_sha, expected_size = PROBE_FIXTURES[component]
        if path.stat().st_size != expected_size or digest(path) != expected_sha:
            raise RuntimeError("The bundled runtime probe fixture failed its pinned integrity check.")
    return path.resolve()


def run(component: str) -> dict:
    import platform

    result = {"schema_version": 1, "component": component, "success": False,
              "inference_completed": False, "identity": None, "backend": None,
              "device": None, "duration_seconds": None, "measurements": {}}
    start = time.monotonic()
    if component == "silero":
        import numpy as np
        from speechcraft_dataset.vr_slicer import compute_vad_frames
        from speechcraft_dataset.runtime_tools import verify_managed_model

        model = Path(os.environ.get("SPEECHCRAFT_SILERO_ONNX_PATH", ""))
        if not model.is_file() or model.is_symlink():
            raise RuntimeError("Verified Silero ONNX path is unavailable.")
        verify_managed_model("speech_detection", model.parent, os.environ.get("SPEECHCRAFT_MODEL_PLATFORM", "linux-x86_64"))
        import soundfile as sf
        recording = audio_file("silero")
        samples, rate = sf.read(str(recording), dtype="float32", always_2d=True)
        if rate != 16_000 or samples.shape[1] != 1 or len(samples) < 16_000:
            raise RuntimeError("The pinned Silero fixture must be nonempty 16 kHz mono audio.")
        samples = samples[:, 0]
        rows = compute_vad_frames(samples, 16_000)
        values = np.asarray([row["speech_prob"] for row in rows], dtype=np.float64)
        if values.size == 0 or not np.isfinite(values).all() or float(values.max()) < 0.25:
            raise RuntimeError("Silero inference did not produce a finite positive speech response for the bundled speech fixture.")
        leading = [float(row["speech_prob"]) for row in rows if float(row["center_sec"]) < 0.75]
        trailing = [float(row["speech_prob"]) for row in rows if float(row["center_sec"]) > 19.25]
        silence_max = max(leading + trailing, default=0.0)
        result.update(identity=f"silero-onnx-sha256:{digest(model)}", backend="onnxruntime-cpu",
                      device="cpu", inference_completed=True,
                      measurements={"frames": int(values.size), "probability_min": float(values.min()),
                                    "probability_max": float(values.max()), "silence_probability_max": silence_max,
                                    "fixture_sha256": digest(recording), "sample_rate": int(rate)})
    elif component == "whisper":
        from faster_whisper import WhisperModel
        from speechcraft_dataset.runtime_tools import verify_managed_model
        import soundfile as sf

        model = Path(os.environ.get("SPEECHCRAFT_ASR_MODEL_PATH", ""))
        recording = audio_file("whisper")
        recording_duration = float(sf.info(str(recording)).duration)
        if not model.is_dir() or model.is_symlink() or not (model / "model.bin").is_file():
            raise RuntimeError("Verified local Whisper Large-v3 model directory is unavailable.")
        verify_managed_model("asr", model, os.environ.get("SPEECHCRAFT_MODEL_PLATFORM", "linux-x86_64"))
        device = os.environ.get("SPEECHCRAFT_PROBE_DEVICE", "cpu")
        compute = "float16" if device == "cuda" else "int8"
        engine = WhisperModel(str(model), device=device, compute_type=compute, local_files_only=True)
        segments, info = engine.transcribe(str(recording), beam_size=1)
        rows = list(segments)
        text_length = sum(len(row.text.strip()) for row in rows)
        if not rows or text_length == 0:
            raise RuntimeError("Whisper inference completed without a nonempty transcript.")
        result.update(identity=f"whisper-large-v3-ct2:{digest(model / 'model.bin')}",
                      backend=f"faster-whisper/ctranslate2:{compute}", device=device,
                      inference_completed=True, measurements={"segments": len(rows), "transcript_characters": text_length,
                                                              "language": info.language,
                                                              "recording_duration_seconds": recording_duration})
    elif component == "titanet":
        import numpy as np
        import soundfile as sf
        from speechcraft_dataset.eval_speaker_purity import embedding_vector, load_titanet_model

        raw = Path(os.environ.get("SPEECHCRAFT_TITANET_ONNX_PATH", ""))
        if os.environ.get("SPEECHCRAFT_EXPERIMENTAL_TITANET") != "1":
            raise RuntimeError("TitaNet probe is disabled: licensing/checkpoint approval is unresolved.")
        recording = audio_file("titanet")
        samples, rate = sf.read(str(recording), dtype="float32", always_2d=True)
        mono = samples.mean(axis=1)
        if rate != 16_000:
            raise RuntimeError("TitaNet probe audio must be 16 kHz.")
        model = load_titanet_model("titanet-large-speaker-purity", "cpu", raw)
        first, _ = model.infer_segment(mono)
        second, _ = model.infer_segment(mono)
        norm = float(np.linalg.norm(first))
        normalized = embedding_vector(first)
        normalized_second = embedding_vector(second)
        normalized_norm = float(np.linalg.norm(normalized))
        similarity = float(np.dot(normalized, normalized_second))
        if first.size != model.dimension or not np.isfinite(first).all() or norm <= 1e-12 or abs(normalized_norm - 1.0) > 1e-5 or similarity < 0.99:
            raise RuntimeError("TitaNet output failed shape, finiteness, norm, or repeatability checks.")
        result.update(identity=f"experimental-titanet-onnx-sha256:{digest(raw)}", backend="sherpa-onnx",
                      device="cpu", inference_completed=True,
                      measurements={"embedding_dimension": int(first.size), "raw_l2_norm": norm,
                                    "normalized_l2_norm": normalized_norm,
                                    "repeat_cosine_similarity": similarity,
                                    "release_blocked": True})
    elif component == "pyannote":
        import soundfile as sf
        from speechcraft_dataset.diarization import load_community1_pipeline, run_community1_pipeline
        import numpy as np

        snapshot = Path(os.environ.get("SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH", ""))
        recording = audio_file("pyannote")
        samples, rate = sf.read(str(recording), dtype="float32", always_2d=True)
        ready, reason = __import__("speechcraft_dataset.diarization", fromlist=["community1_snapshot_ready"]).community1_snapshot_ready(snapshot)
        if not ready:
            raise RuntimeError(f"model_snapshot_missing: {reason}")
        device = os.environ.get("SPEECHCRAFT_PROBE_DEVICE", "cpu")
        pipeline = load_community1_pipeline(snapshot, device)
        turns = run_community1_pipeline(pipeline, np.ascontiguousarray(samples.T), int(rate))
        if not turns or any(float(row["start_sec"]) < 0 or float(row["end_sec"]) <= float(row["start_sec"])
                            or not row.get("speaker_id") for row in turns):
            raise RuntimeError("Community-1 inference returned no diarization turns.")
        result.update(identity=f"pyannote-community-1-local:{snapshot.name}", backend="pyannote.audio",
                      device=device, inference_completed=True,
                      measurements={"turn_count": len(turns), "speaker_count": len({row["speaker_id"] for row in turns}),
                                    "recording_duration_seconds": round(len(samples) / int(rate), 4)})
    elif component == "ffmpeg":
        import soundfile as sf

        recording = audio_file("ffmpeg")
        ffmpeg = Path(os.environ.get("SPEECHCRAFT_FFMPEG_BIN", ""))
        ffprobe = Path(os.environ.get("SPEECHCRAFT_FFPROBE_BIN", ""))
        if not ffmpeg.is_file() or not ffprobe.is_file() or ffmpeg.is_symlink() or ffprobe.is_symlink():
            raise RuntimeError("Verified managed FFmpeg/FFprobe executables are unavailable.")
        ffmpeg_version = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True,
                                        timeout=30, check=True).stdout.splitlines()[0]
        ffprobe_version = subprocess.run([str(ffprobe), "-version"], capture_output=True, text=True,
                                         timeout=30, check=True).stdout.splitlines()[0]
        probe = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(recording)],
                               capture_output=True, text=True, timeout=30, check=True)
        source_duration = float(json.loads(probe.stdout)["format"]["duration"])
        import tempfile
        with tempfile.TemporaryDirectory(prefix="speechcraft-ffmpeg-probe-") as directory:
            output = Path(directory) / "decoded.wav"
            subprocess.run([str(ffmpeg), "-nostdin", "-v", "error", "-y", "-i", str(recording), "-ac", "1", "-ar", "16000", str(output)],
                           capture_output=True, timeout=60, check=True)
            audio, rate = sf.read(str(output), dtype="float32", always_2d=True)
            if not len(audio) or rate != 16_000:
                raise RuntimeError("Managed FFmpeg produced empty audio or the wrong sample rate.")
            decoded_duration = len(audio) / rate
            if abs(decoded_duration - source_duration) > max(0.1, source_duration * 0.01):
                raise RuntimeError("Managed FFmpeg decode duration differs from ffprobe by more than tolerance.")
        result.update(identity=f"ffmpeg-sha256:{digest(ffmpeg)}", backend="ffmpeg/ffprobe",
                      device="cpu", inference_completed=True,
                      measurements={"sample_rate": int(rate), "channels": int(audio.shape[1]),
                                    "samples": int(len(audio)), "source_duration_seconds": source_duration,
                                    "decoded_duration_seconds": decoded_duration, "ffmpeg_version": ffmpeg_version,
                                    "ffprobe_version": ffprobe_version})
    elif component == "cuda":
        import ctypes
        import torch
        import ctypes.util

        result.update(driver_identification=os.environ.get("SPEECHCRAFT_CUDA_DRIVER", "")[:160] or None,
                      gpu_detected=False, torch_cuda_success=False, ctranslate2_cuda_success=False,
                      actual_execution_device=None, required_libraries={"cublas": False, "cudnn": False, "cudart": False},
                      fallback_decision="pending-controller-decision")
        try:
            result["gpu_detected"] = bool(torch.cuda.is_available())
            if not result["gpu_detected"]:
                raise RuntimeError("PyTorch reports no usable NVIDIA device.")
            gpu_name = torch.cuda.get_device_name(0)
            tensor = torch.ones((256, 256), device="cuda")
            _ = tensor @ tensor
            torch.cuda.synchronize()
            result["torch_cuda_success"] = True
            result["actual_execution_device"] = gpu_name
            required = {"cublas": "libcublas.so.12", "cudnn": "libcudnn.so.9", "cudart": "libcudart.so.12"}
            for name, library in required.items():
                try:
                    ctypes.CDLL(library)
                    result["required_libraries"][name] = True
                except OSError:
                    result["required_libraries"][name] = False
            if not all(result["required_libraries"].values()):
                raise RuntimeError("Managed CUDA libraries are not loadable from the trusted worker environment.")
            import ctranslate2
            cuda_devices = int(ctranslate2.get_cuda_device_count())
            result["ctranslate2_cuda_success"] = cuda_devices > 0
            if not result["ctranslate2_cuda_success"]:
                raise RuntimeError("CTranslate2 reports no usable CUDA devices.")
            from faster_whisper import WhisperModel
            model_path = Path(os.environ.get("SPEECHCRAFT_ASR_MODEL_PATH", ""))
            recording = audio_file("cuda")
            if not model_path.is_dir() or model_path.is_symlink() or not (model_path / "model.bin").is_file():
                raise RuntimeError("Verified local Whisper Large-v3 model directory is unavailable for CUDA validation.")
            engine = WhisperModel(str(model_path), device="cuda", compute_type="float16", local_files_only=True)
            segments, info = engine.transcribe(str(recording), beam_size=1)
            rows = list(segments)
            text_length = sum(len(row.text.strip()) for row in rows)
            if not rows or text_length == 0:
                raise RuntimeError("CTranslate2 CUDA Whisper inference returned no nonempty transcript.")
            result.update(identity=f"cuda:{gpu_name}", backend=f"torch:{torch.__version__}/ctranslate2:{ctranslate2.__version__}",
                          device=gpu_name, actual_execution_device=gpu_name, inference_completed=True,
                          success=True, torch_cuda_success=True, ctranslate2_cuda_success=True,
                          fallback_decision="cuda-validated",
                          measurements={"torch_cuda": torch.version.cuda, "ctranslate2_cuda_devices": cuda_devices,
                                        "whisper_compute_type": "float16", "whisper_segments": len(rows),
                                        "transcript_characters": text_length, "language": info.language,
                                        "fixture_sha256": digest(recording)})
        except Exception as exc:
            result.update(success=False, inference_completed=False, duration_seconds=round(time.monotonic() - start, 4),
                          error_code="cuda_runtime_unavailable",
                          error="CUDA inference did not pass PyTorch, library, CTranslate2, and local Whisper checks; the controller should retain the validated CPU profile.",
                          diagnostic=f"{type(exc).__name__}: {str(exc)[:180]}", fallback_decision="select-cpu-if-verified")
            return result
    else:
        raise RuntimeError("Unknown trusted probe component.")
    result.update(success=True, duration_seconds=round(time.monotonic() - start, 4), host_architecture=platform.machine())
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("ffmpeg", "silero", "whisper", "titanet", "pyannote", "cuda"))
    args = parser.parse_args()
    try:
        # Third-party inference libraries may print banners; reserve stdout for
        # exactly one machine-readable response.
        with contextlib.redirect_stdout(sys.stderr):
            result = run(args.component)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("success") is True else 1
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"[:320]
        lower = message.lower()
        error_code = ("model_snapshot_missing" if "model_snapshot_missing" in lower or "snapshot is missing" in lower
                      else "model_release_blocked" if "authorization" in lower or "licensing/checkpoint" in lower
                      else "probe_audio_missing" if "probe recording is required" in lower
                      else "cuda_runtime_unavailable" if "cuda" in lower or "cublas" in lower
                      else "runtime_tool_unavailable" if "managed ffmpeg" in lower or "ffprobe executable" in lower
                      else "runtime_probe_failed")
        safe_message = {
            "model_snapshot_missing": "Authorized local Community-1 snapshot is missing or incomplete.",
            "model_release_blocked": "TitaNet remains blocked pending licensing and checkpoint approval.",
            "probe_audio_missing": "Provide a local intelligible recording through SPEECHCRAFT_RUNTIME_PROBE_AUDIO.",
            "cuda_runtime_unavailable": "CUDA inference failed in the managed worker environment; the validated CPU profile remains available.",
            "runtime_tool_unavailable": "A trusted managed FFmpeg/FFprobe executable is not installed.",
        }.get(error_code, f"{args.component} inference probe failed ({type(exc).__name__}); inspect the local controller log.")
        print(json.dumps({"schema_version": 1, "component": args.component, "success": False,
                          "inference_completed": False, "error_code": error_code,
                          "error": safe_message}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
