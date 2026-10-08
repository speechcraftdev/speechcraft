#!/usr/bin/env python3
"""Extract embeddings and run the production SP scoring helpers, one backend per process."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workers/dataset"))
from speechcraft_dataset.eval_speaker_purity import (  # noqa: E402
    DEFAULT_MODEL, SpeakerPurityConfig, embedding_vector, iter_sliding_windows,
    outlier_guillotine, prepare_window_samples, score_candidate_clip,
    window_is_scorable,
)

EXPECTED_ONNX_SHA256 = "d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77"
EXPECTED_DIM = 192
SR = 16000
CFG = SpeakerPurityConfig()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

def ordered_rows(rows: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: tuple(row[field] for field in fields))


def read_audio(path: Path) -> np.ndarray:
    samples, rate = sf.read(path, dtype="float32", always_2d=False)
    if rate != SR or samples.ndim != 1:
        raise ValueError(f"expected mono {SR} Hz audio: {path} ({rate}, {samples.shape})")
    if not np.isfinite(samples).all():
        raise ValueError(f"input has non-finite samples: {path}")
    return samples


def digest(samples: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(samples, dtype=np.float32).tobytes()).hexdigest()

def validate_embedding(raw: Any) -> np.ndarray:
    vector = np.asarray(raw, dtype=np.float64).reshape(-1)
    if vector.size != EXPECTED_DIM or not np.isfinite(vector).all():
        raise ValueError(f"invalid embedding: shape={vector.shape}, finite={np.isfinite(vector).all()}")
    if np.linalg.norm(vector) <= 1e-12:
        raise ValueError("zero embedding vector")
    return vector

def flagged_at_legacy_70(raw_score: float | None) -> bool | None:
    return None if raw_score is None else round(float(raw_score) * 100.0, 2) < 70.0


class Provider:
    def __init__(self, backend: str, model_path: Path | None):
        self.backend = backend
        self.calls = 0
        self.inference_times: list[float] = []
        if backend == "nemo":
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            import torch
            from nemo.collections.asr.models import EncDecSpeakerLabelModel
            self.torch = torch
            self.model = EncDecSpeakerLabelModel.from_pretrained(DEFAULT_MODEL, map_location="cpu")
            self.model.eval()
            self.model.to("cpu")
            self.version = __import__("nemo").__version__
        else:
            if model_path is None or not model_path.is_file():
                raise FileNotFoundError("--model must name a local ONNX file; implicit downloads are disabled")
            sha = hashlib.sha256(model_path.read_bytes()).hexdigest()
            if sha != EXPECTED_ONNX_SHA256:
                raise ValueError(f"ONNX SHA256 mismatch: {sha}")
            import sherpa_onnx
            self.sherpa_onnx = sherpa_onnx
            self.model = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(model_path), num_threads=max(1, os.cpu_count() or 1), debug=False, provider="cpu"
                )
            )
            if not self.model:
                raise RuntimeError("sherpa-onnx could not load the model")
            self.version = getattr(sherpa_onnx, "__version__", "unknown")

    def embed(self, samples: np.ndarray) -> np.ndarray:
        audio = np.ascontiguousarray(samples, dtype=np.float32)
        self.calls += 1
        call_started = time.perf_counter()
        if self.backend == "nemo":
            with self.torch.inference_mode():
                emb, _ = self.model.infer_segment(audio)
            raw = emb.detach().cpu().numpy() if hasattr(emb, "detach") else emb
        else:
            stream = self.model.create_stream()
            stream.accept_waveform(SR, audio)
            raw = self.model.compute(stream)
        self.inference_times.append(time.perf_counter() - call_started)
        vector = validate_embedding(raw)
        return vector


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--backend", choices=("nemo", "sherpa"), required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", type=Path)
    p.add_argument("--diagnostic-chunks", type=int, default=160)
    args = p.parse_args()
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    clips = ordered_rows(read_jsonl(args.corpus / "clips.jsonl"), "source_id", "clip_id")
    sources = sorted((args.corpus / "sources").glob("*/source.json"))
    source_records = {x["source_id"]: x for x in (json.loads(f.read_text()) for f in sources)}
    enroll: list[tuple[str, dict[str, Any], Path]] = []
    for source_id in sorted(source_records):
        rows = ordered_rows(read_jsonl(args.corpus / "sources" / source_id / "enrollment_chunks.jsonl"), "chunk_id")
        for row in rows:
            path = Path(row["audio_path"])
            if not path.exists():
                path = args.corpus / "sources" / source_id / "enrollment/all_chunks" / f"{row['chunk_id']}.wav"
            enroll.append((source_id, row, path))
    model_start = time.perf_counter()
    provider = Provider(args.backend, args.model)
    model_load_sec = time.perf_counter() - model_start
    input_hashes: dict[str, str] = {}
    all_vectors: dict[str, np.ndarray] = {}
    errors: list[dict[str, str]] = []
    started = time.perf_counter()
    embedded_audio_sec = 0.0

    def one(key: str, path: Path) -> None:
        nonlocal embedded_audio_sec
        samples = read_audio(path)
        input_hashes[key] = digest(samples)
        embedded_audio_sec += len(samples) / SR
        all_vectors[key] = provider.embed(samples)

    for source_id, row, path in enroll:
        key = f"enroll:{source_id}:{row['chunk_id']}"
        try:
            one(key, path)
        except Exception as exc:
            errors.append({"item_id": key, "error": f"{type(exc).__name__}: {exc}"})

    clip_audio: dict[str, np.ndarray] = {}
    windows_by_clip: dict[str, list[str]] = {}
    clip_lookup: dict[str, dict[str, Any]] = {}
    for clip in clips:
        clip_id = f"{clip['source_id']}/{clip['clip_id']}"
        clip_lookup[clip_id] = clip
        try:
            samples = read_audio(Path(clip["clip_path"]))
            clip_audio[clip_id] = samples
            keys = []
            for ix, (start, end, _sec) in enumerate(iter_sliding_windows(samples, SR, window_sec=CFG.window_sec, hop_sec=CFG.window_hop_sec)):
                window = prepare_window_samples(samples[start:end], SR, CFG.window_sec)
                scorable, _, _ = window_is_scorable(window, SR, CFG)
                if not scorable:
                    continue
                key = f"window:{clip_id}:{ix:04d}"
                input_hashes[key] = digest(window)
                keys.append(key)
                if key not in all_vectors:
                    embedded_audio_sec += len(window) / SR
                    all_vectors[key] = provider.embed(window)
            windows_by_clip[clip_id] = keys
        except Exception as exc:
            errors.append({"item_id": f"clip:{clip_id}", "error": f"{type(exc).__name__}: {exc}"})

    extraction_sec = time.perf_counter() - started
    # Retain a deterministic sample of same-audio embeddings for embedding diagnostics.
    diagnostic_keys = sorted(all_vectors)[: max(0, args.diagnostic_chunks)]
    np.savez_compressed(out / "embeddings.npz", **{k.replace(":", "__"): all_vectors[k].astype(np.float32) for k in all_vectors})
    (out / "embedding_index.json").write_text(json.dumps({"array_key_map": {k.replace(":", "__"): k for k in all_vectors}, "input_hashes": input_hashes, "diagnostic_keys": diagnostic_keys}, indent=2))

    # Keep the exact archived scoring math: same guillotine, centroid, and score_candidate_clip.
    score_rows = []
    enrollment_rows = []
    all_pair = []
    for source_id in sorted(source_records):
        rows = [r for sid, r, _ in enroll if sid == source_id]
        keys = [f"enroll:{source_id}:{r['chunk_id']}" for r in rows]
        valid = [(r, all_vectors[k]) for r, k in zip(rows, keys) if k in all_vectors]
        if len(valid) != len(rows):
            enrollment_rows.append({"source_id": source_id, "chunk_count": len(rows), "valid_count": len(valid), "error": "embedding_failure"})
            continue
        vecs = [embedding_vector(v) for _, v in valid]
        keep, means, cutoff = outlier_guillotine(vecs, percentile=CFG.outlier_percentile)
        centroid = embedding_vector(np.mean(np.stack([v for v, k in zip(vecs, keep) if k]), axis=0))
        old_rows = read_jsonl(args.corpus / "sources" / source_id / "enrollment_chunks.jsonl")
        old_by_id = {r["chunk_id"]: r for r in old_rows}
        kept_ids = [r["chunk_id"] for (r, _), k in zip(valid, keep) if k]
        enrollment_rows.append({"source_id": source_id, "chunk_count": len(rows), "valid_count": len(valid), "kept_count": sum(keep), "killed_count": len(keep)-sum(keep), "cutoff": cutoff, "kept_chunk_ids": kept_ids, "killed_chunk_ids": [r["chunk_id"] for (r,_),k in zip(valid,keep) if not k], "old_kept_chunk_ids": [r["chunk_id"] for r in rows if old_by_id[r["chunk_id"]]["guillotine_status"] == "kept"], "wrong_gold_total": sum(bool(old_by_id[r["chunk_id"]].get("gold_wrong_speaker")) for r in rows), "wrong_gold_kept": sum(bool(old_by_id[r["chunk_id"]].get("gold_wrong_speaker")) for (r,_),k in zip(valid,keep) if k), "centroid": centroid.tolist()})
        for i, (r, v) in enumerate(valid):
            enrollment_rows[-1].setdefault("chunk_details", []).append({"chunk_id":r["chunk_id"],"kept":bool(keep[i]),"old_kept":old_by_id[r["chunk_id"]]["guillotine_status"]=="kept","gold_wrong_speaker":bool(old_by_id[r["chunk_id"]].get("gold_wrong_speaker")),"norm_raw":float(np.linalg.norm(v)),"norm_normalized":float(np.linalg.norm(vecs[i]))})
        class CachedProvider:
            def infer_segment(self, audio):
                key = "window:" + current_clip_key + ":" + str(current_window_ix).zfill(4)
                # score_candidate_clip does not pass a key; sequential iterator below sets one.
                return self.current_vector, None
        for clip_id, clip in clip_lookup.items():
            if not clip_id.startswith(source_id + "/"):
                continue
            raw = clip.get("clip_path")
            samples = clip_audio.get(clip_id)
            if samples is None:
                score_rows.append({"source_id":source_id,"clip_id":clip["clip_id"],"label":not bool(clip["gold_clean"]),"raw_sp_score":None,"failure":"audio_or_window_extraction_failure"})
                continue
            class QueueProvider:
                def __init__(self, vectors): self.vectors=iter(vectors)
                def infer_segment(self, audio): return next(self.vectors), None
            vectors = [all_vectors[k] for k in windows_by_clip[clip_id]]
            cached = QueueProvider(vectors)
            try:
                result = score_candidate_clip(cached, samples, SR, centroid, CFG)
                score_rows.append({"source_id":source_id,"clip_id":clip["clip_id"],"label":not bool(clip["gold_clean"]),"leak_sec":float(clip.get("non_target_leak_sec") or 0),"raw_sp_score":result["purity_score"],"sp70_flagged":flagged_at_legacy_70(result["purity_score"]),"scored_windows":result["scored_window_count"],"failure":None if result["purity_score"] is not None else "no_scorable_windows"})
            except Exception as exc:
                score_rows.append({"source_id":source_id,"clip_id":clip["clip_id"],"label":not bool(clip["gold_clean"]),"raw_sp_score":None,"failure":f"{type(exc).__name__}: {exc}"})
    scored_ids = {(r["source_id"], r["clip_id"]) for r in score_rows}
    for clip in clips:
        pair = (clip["source_id"], clip["clip_id"])
        if pair not in scored_ids:
            score_rows.append({"source_id":pair[0],"clip_id":pair[1],"label":not bool(clip["gold_clean"]),"leak_sec":float(clip.get("non_target_leak_sec") or 0),"raw_sp_score":None,"sp70_flagged":None,"failure":"source_enrollment_failed_or_scoring_not_reached"})
    score_rows.sort(key=lambda r:(r["source_id"],r["clip_id"]))
    (out / "per_clip_scores.jsonl").write_text("".join(json.dumps(r)+"\n" for r in score_rows))
    for row in enrollment_rows:
        row.pop("centroid", None)
    (out / "enrollment_diagnostics.json").write_text(json.dumps(enrollment_rows, indent=2))
    if args.backend == "sherpa":
        model_sha = hashlib.sha256(args.model.read_bytes()).hexdigest()
        model_size = args.model.stat().st_size
        model_revision = "0743f30 (Hugging Face file page commit)"
    else:
        cache_blob = Path.home()/".cache/huggingface/hub/models--nvidia--speakerverification_en_titanet_large/blobs/e838520693f269e7984f55bc8eb3c2d60ccf246bf4b896d4be9bcabe3e4b0fe3"
        model_sha = hashlib.sha256(cache_blob.read_bytes()).hexdigest() if cache_blob.is_file() else None
        model_size = cache_blob.stat().st_size if cache_blob.is_file() else None
        ref = Path.home()/".cache/huggingface/hub/models--nvidia--speakerverification_en_titanet_large/refs/main"
        model_revision = ref.read_text().strip() if ref.is_file() else None
    packages = {}
    for package in (("nemo_toolkit", "torch", "numpy", "scipy", "soundfile") if args.backend=="nemo" else ("sherpa-onnx", "onnxruntime", "numpy", "scipy", "soundfile")):
        try:
            dist=importlib.metadata.distribution(package)
            packages[package]={"version":dist.version,"installed_file_bytes":sum((dist.locate_file(f).stat().st_size for f in (dist.files or []) if dist.locate_file(f).is_file()),0)}
        except importlib.metadata.PackageNotFoundError:
            packages[package]={"version":None,"installed_file_bytes":None}
    metadata = {"backend":args.backend,"model_name": DEFAULT_MODEL if args.backend=="nemo" else str(args.model),"model_revision":model_revision,"model_sha256":model_sha,"model_disk_bytes":model_size,"runtime_version":provider.version,"package_footprint":packages,"embedding_dim":EXPECTED_DIM,"sample_rate":SR,"precision":"FP32","device":"cpu","model_load_sec":model_load_sec,"model_load_cold_process":True,"filesystem_cache_state":"warm after download/cache access; not a cold disk measurement","embedding_extraction_and_scoring_sec":extraction_sec,"embedded_audio_seconds":embedded_audio_sec,"embedding_throughput_audio_seconds_per_wall_second":embedded_audio_sec/extraction_sec if extraction_sec else None,"embedding_count":len(all_vectors),"embedding_failures":errors,"first_inference_sec":provider.inference_times[0] if provider.inference_times else None,"warm_inference_median_sec":float(np.median(provider.inference_times[1:])) if len(provider.inference_times)>1 else None,"process_peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,"corpus_clip_count":len(clips),"sources":len(source_records)}
    (out / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps({"clips":len(score_rows),"scored":sum(r.get("raw_sp_score") is not None for r in score_rows),"failures":len(errors),"metadata":metadata}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
