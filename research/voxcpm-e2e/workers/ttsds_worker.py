"""Run the official TTSDS BenchmarkSuite in its isolated worker process."""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import sys
import traceback
from pathlib import Path
from typing import NamedTuple
from importlib.metadata import version, PackageNotFoundError

payload = json.load(sys.stdin)
if payload.get("cache_dir"):
    os.environ["TTSDS_CACHE_DIR"] = payload["cache_dir"]


def json_value(value):
    """Convert pandas/numpy values while keeping NaN explicit as JSON null."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def _has_dist(name):
    try:
        version(name)
        return True
    except PackageNotFoundError:
        return False


def ttsds_checkpoint_provenance(package_root, benchmark_objects):
    """Record upstream benchmark configs and resolved HF revisions if cached."""
    benchmark_specs = []
    for benchmark in benchmark_objects:
        benchmark_specs.append({
            "name": benchmark.name,
            "category": benchmark.category.name,
            "version": benchmark.version,
            "parameters": json_value(getattr(benchmark, "kwargs", {})),
        })
    candidates = set()
    repo_pattern = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    for source in Path(package_root).rglob("*.py"):
        try:
            import ast
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and repo_pattern.fullmatch(node.value):
                    candidates.add(node.value)
        except Exception:
            continue
    revisions = []
    try:
        from huggingface_hub import scan_cache_dir
        for repo in scan_cache_dir().repos:
            if repo.repo_id in candidates:
                revisions.append({"repo_id": repo.repo_id,
                                  "cached_revisions": sorted({r.commit_hash for r in repo.revisions})})
    except Exception:
        pass
    return {"benchmarks": benchmark_specs, "huggingface_checkpoints": sorted(revisions, key=lambda x: x["repo_id"])}


def install_torchaudio_compatibility():
    """Bridge removed torchaudio APIs for upstream TTSDS dependencies.

    Torchaudio 2.10 removed backend selection/info and Sox effect APIs while
    Wespeaker/S3PRL still imports them. TTSDS receives staged WAV files, so
    SoundFile provides metadata and decoding without changing the evaluator.
    Sox transforms fail explicitly if an upstream evaluator requests them.
    """
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    import types

    if not hasattr(torchaudio, "AudioMetaData"):
        class AudioMetaData(NamedTuple):
            sample_rate: int
            num_frames: int
            num_channels: int
            bits_per_sample: int
            encoding: str
        torchaudio.AudioMetaData = AudioMetaData
    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["soundfile"]
    if not hasattr(torchaudio, "set_audio_backend"):
        torchaudio.set_audio_backend = lambda backend: None
    if not hasattr(torchaudio, "info"):
        def info(path, backend=None):
            x = sf.info(path)
            return torchaudio.AudioMetaData(x.samplerate, x.frames, x.channels, 16, "PCM_S")
        torchaudio.info = info
    if not hasattr(torchaudio, "load") or "torchcodec" in str(getattr(torchaudio.load, "__module__", "")):
        def load(path, frame_offset=0, num_frames=-1, normalize=True,
                 channels_first=True, format=None, backend=None):
            frames = None if num_frames is None or num_frames < 0 else num_frames
            audio, sr = sf.read(path, start=frame_offset, frames=frames,
                                dtype="float32", always_2d=True)
            tensor = torch.from_numpy(np.ascontiguousarray(audio.T))
            return (tensor if channels_first else tensor.T.contiguous()), int(sr)
        torchaudio.load = load
    if "torchaudio.sox_effects" not in sys.modules:
        module = types.ModuleType("torchaudio.sox_effects")
        def unsupported_sox_effects(*args, **kwargs):
            raise RuntimeError("TTSDS dependency requested Sox effects, unavailable in this isolated runtime")
        module.apply_effects_tensor = unsupported_sox_effects
        module.apply_effects_file = unsupported_sox_effects
        sys.modules["torchaudio.sox_effects"] = module
    return {"soundfile_wav_loader": True, "torchaudio_backend_selection_noop": True,
            "sox_effects": "explicit error if requested"}


def install_huggingface_hub_compatibility():
    """Translate the pre-v1 auth keyword still passed by Pyannote 3.4."""
    import huggingface_hub
    original = huggingface_hub.hf_hub_download
    if getattr(original, "_speechcraft_compat", False):
        return {"legacy_use_auth_token": "translated to token"}
    def compatible_hf_hub_download(*args, use_auth_token=None, token=None, **kwargs):
        if token is None and use_auth_token is not None:
            token = use_auth_token
        return original(*args, token=token, **kwargs)
    compatible_hf_hub_download._speechcraft_compat = True
    huggingface_hub.hf_hub_download = compatible_hf_hub_download
    return {"legacy_use_auth_token": "translated to token"}


def install_torch_checkpoint_compatibility():
    """Allowlist metadata classes stored in the official Pyannote checkpoint."""
    import torch
    from pyannote.audio.core.task import Problem, Resolution, Specifications
    safe = [torch.torch_version.TorchVersion, Specifications, Problem, Resolution]
    torch.serialization.add_safe_globals(safe)
    return {"safe_globals": [f"{x.__module__}.{x.__name__}" for x in safe]}


out = {}
logs = io.StringIO()
try:
    compat = {"torchaudio": install_torchaudio_compatibility(),
              "huggingface_hub": install_huggingface_hub_compatibility(),
              "torch_checkpoint": install_torch_checkpoint_compatibility()}
    with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
        import numpy as np
        import pandas as pd
        import torch
        import torchaudio
        from ttsds import BenchmarkSuite
        from ttsds.util.dataset import DirectoryDataset
        from ttsds.benchmarks.benchmark import BenchmarkCategory

        generated = DirectoryDataset(payload["generated_dir"], name=payload.get("condition", "generated"))
        reference = DirectoryDataset(payload["reference_dir"], name="real_reference")
        weights = {
            BenchmarkCategory.SPEAKER: .25,
            BenchmarkCategory.PROSODY: .25,
            BenchmarkCategory.INTELLIGIBILITY: .25,
            BenchmarkCategory.GENERIC: .25,
            BenchmarkCategory.ENVIRONMENT: 0.0,
        }
        suite = BenchmarkSuite(
            datasets=[generated], reference_datasets=[reference],
            include_environment=False, category_weights=weights,
            skip_errors=True,
            write_to_file=payload.get("individual_csv"),
            device=payload.get("device", "cpu"),
            cache_dir=payload.get("cache_dir"), n_workers=1,
        )
        raw = suite.run()
        aggregated = suite.get_aggregated_results()
        checkpoint_provenance = ttsds_checkpoint_provenance(
            Path(__import__("ttsds").__file__).resolve().parent,
            suite.benchmarks.values(),
        )

        def frame_records(frame):
            return json_value(frame.to_dict(orient="records")) if hasattr(frame, "to_dict") else json_value(frame)

        raw_records = frame_records(raw)
        aggregated_records = frame_records(aggregated)
        condition = payload.get("condition", "generated")
        categories = ("Speaker", "Prosody", "Intelligibility", "Generic")
        category_scores = {
            cat: next((r.get("score_mean") for r in aggregated_records
                       if str(r.get("benchmark_category", "")).casefold() == cat.casefold()
                       and str(r.get("dataset", "")) == condition), None)
            for cat in categories
        }
        all_raw_scores_finite = bool(raw_records) and all(
            isinstance(row.get("score"), (int, float)) and math.isfinite(row["score"])
            for row in raw_records
        )
        complete_categories = all(
            isinstance(value, (int, float)) and math.isfinite(value)
            for value in category_scores.values()
        )
        upstream_overall = next((r.get("score_mean") for r in aggregated_records
                                 if str(r.get("benchmark_category", "")).upper() == "OVERALL"
                                 and str(r.get("dataset", "")) == condition), None)
        overall = upstream_overall if complete_categories and all_raw_scores_finite else None
        out = {
            "version": version("ttsds"),
            "numpy": np.__version__, "torch": torch.__version__,
            "torchaudio": torchaudio.__version__,
            "dependencies": {
                name: version(name) for name in (
                    "ttsds", "numpy", "torch", "torchaudio", "transformers",
                    "pyannote-audio", "pyworld", "allosaurus", "wespeaker-unofficial",
                    "jiwer", "SpeechBrain", "faster-whisper", "silero-vad",
                ) if _has_dist(name)
            },
            "cuda_available": torch.cuda.is_available(),
            "device": payload.get("device", "cpu"),
            "compatibility": compat,
            "weights": {"Speaker": .25, "Prosody": .25, "Intelligibility": .25, "Generic": .25, "Environment": 0.0},
            "environment_included": False,
            "checkpoint_provenance": checkpoint_provenance,
            "raw": raw_records,
            "aggregated": aggregated_records,
            "category_scores": category_scores,
            "overall_score": overall,
            "complete": complete_categories and all_raw_scores_finite,
            "individual_errors": [r for r in raw_records if r.get("score") is None],
            "upstream_log": logs.getvalue(),
        }
except BaseException as exc:
    out = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
           "upstream_log": logs.getvalue()}
print(json.dumps(json_value(out), ensure_ascii=False, allow_nan=False, default=str))
