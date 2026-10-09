"""Compare legacy ASR+MFA with production VR on the frozen four-recording subset."""
from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT.parent / "buckeye-slicer-lab"
WORKER = ROOT / "workers/dataset"
OLD_SSD_HOME = Path("/run/media/aaravthegreat/2106f036-64ca-4106-8672-f466957a698a/@home/aaravthegreat")
if OLD_SSD_HOME.is_dir():
    os.environ.setdefault("HF_HOME", str(OLD_SSD_HOME / ".cache/huggingface"))
    os.environ.setdefault("SPEECHCRAFT_MFA_BIN", str(Path.home() / ".conda/envs/speechcraft-mfa/bin/mfa"))
    os.environ.setdefault("SPEECHCRAFT_MFA_ROOT_DIR", str(ROOT / "tmp/mfa_model_root"))
sys.path[:0] = [str(LAB), str(ROOT.parent / "speaker_ts_eval/src")]

from adapter.buckeye_loader import BuckeyePaths, load_recording
from referee.evaluate import evaluate
from referee.types import Clip, Cutpoint, SlicerResult
from speechcraft_dataset import vr_slicer
import speaker_ts_eval.repaired_buckeye_benchmark as bench


SUBSET = (("s01", "s0101b"), ("s02", "s0201b"), ("s03", "s0301a"), ("s04", "s0401b"))
NEMO_ROOT = Path("/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_nemo_batch_v1/runs")
OUT = ROOT / "eval_runs/slicer_head_to_head_2026-10-07"


def csv_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def score_saved_tables(reference, tables: Path) -> dict:
    cuts = csv_rows(tables / "selected_boundaries.csv")
    clips = csv_rows(tables / "emitted_clips.csv")
    result = SlicerResult(
        tuple(Cutpoint(r["recording_id"], r["buffer_id"], float(r["time_sec"])) for r in cuts),
        tuple(Clip(r["recording_id"], r["buffer_id"], float(r["start_sec"]), float(r["end_sec"])) for r in clips),
    )
    return {"metrics": asdict(evaluate(reference, result)), "cut_count": len(cuts), "clip_count": len(clips)}


def install_legacy_worker_overlay():
    # ASR/MFA/SafeCut were removed when production switched to VR. Recover the
    # exact shipped files into the run artifact; do not put legacy code back in production.
    overlay = OUT / "legacy_worker_overlay"
    package = overlay / "speechcraft_dataset"
    package.mkdir(parents=True, exist_ok=True)
    paths = __import__("subprocess").run(
        ["git", "ls-tree", "-r", "--name-only", "be7b712^", "workers/dataset/speechcraft_dataset"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    for old_path in paths:
        name = old_path.removeprefix("workers/dataset/speechcraft_dataset/")
        if not name.endswith(".py"):
            continue
        blob = __import__("subprocess").run(
            ["git", "show", f"be7b712^:{old_path}"], cwd=ROOT, check=True, capture_output=True,
        ).stdout
        destination = package / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)
    # The historical worker parses MFA TextGrids with praatio, which shipped in
    # the MFA environment but is not a production-worker dependency.
    praatio = Path.home() / ".conda/envs/speechcraft-mfa/lib/python3.14/site-packages/praatio"
    if praatio.is_dir():
        shutil.copytree(praatio, overlay / "praatio", dirs_exist_ok=True)
    sys.path.insert(0, str(overlay))
    for name in list(sys.modules):
        if name == "speechcraft_dataset" or name.startswith("speechcraft_dataset."):
            del sys.modules[name]
    from speechcraft_dataset.alignment_qc import run_alignment_qc
    from speechcraft_dataset.asr import build_asr_queue, run_asr
    from speechcraft_dataset.io import sha256_file
    from speechcraft_dataset.mfa import run_mfa_alignment
    from speechcraft_dataset.normalization import normalize_transcripts
    from speechcraft_dataset.safecut import generate_safe_cutpoint_diagnostics

    def load_functions():
        return {"build_asr_queue": build_asr_queue, "run_asr": run_asr,
                "normalize_transcripts": normalize_transcripts,
                "run_mfa_alignment": run_mfa_alignment,
                "run_alignment_qc": run_alignment_qc,
                "generate_safe_cutpoint_diagnostics": generate_safe_cutpoint_diagnostics,
                "worker_sha256_file": sha256_file}
    bench._load_speechcraft_worker_functions = load_functions
    return overlay


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    overlay = install_legacy_worker_overlay()
    paths = BuckeyePaths.canonical()
    rows = []
    for speaker, recording in SUBSET:
        print(f"[{speaker}/{recording}] loading frozen audio and scope", flush=True)
        loaded = load_recording(speaker, recording, paths=paths)
        vr = vr_slicer.slice_wav(
            loaded.audio_path, recording_id=recording, sample_rate_hz=loaded.sample_rate_hz,
            buffers=[vr_slicer.BufferScope(b.buffer_id, b.start_sec, b.end_sec) for b in loaded.buffers],
        )
        vr_scored = evaluate(
            loaded.reference,
            SlicerResult(
                tuple(Cutpoint(c.recording_id, c.buffer_id, c.time_sec) for c in vr.selected_cutpoints),
                tuple(Clip(c.recording_id, c.buffer_id, c.start_sec, c.end_sec) for c in vr.clips),
            ),
        )
        run_dir = NEMO_ROOT / speaker / recording
        print(f"[{speaker}/{recording}] running legacy ASR + MFA + SafeCut", flush=True)
        start = time.monotonic()
        out_root = OUT / "asr_mfa_runs" / speaker
        if out_root.exists():
            for stale in out_root.glob("2026-07-17_000000_buckeye_*_repaired_slicer_dry_run_production_safecut_optimal"):
                if not (stale / "speechcraft_run/artifacts/safe_cutpoints.jsonl").is_file():
                    shutil.rmtree(stale)
        run_root = bench.run_repaired_production_dry_run(
            eval_run=run_dir, normalized_speaker_dir=paths.normalized_root / speaker,
            cluster_mapping_csv=paths.cluster_mapping_csv,
            out_root=out_root,
            shared_cache_root=OUT / "shared_cache", write_heavy_tables=True,
        )
        asr_mfa = score_saved_tables(loaded.reference, run_root / "tables")
        provenance = json.loads((run_root / "provenance.json").read_text())
        stages = provenance.get("speechcraft_stage_summaries", {})
        if not (run_root / "speechcraft_run/artifacts/aligned_words.jsonl").is_file():
            raise RuntimeError(f"MFA aligned words missing for {speaker}/{recording}")
        rows.append({
            "speaker_id": speaker, "recording_id": recording,
            "vr": {"metrics": asdict(vr_scored), "cut_count": len(vr.selected_cutpoints), "clip_count": len(vr.clips)},
            "asr_mfa": asr_mfa,
            "legacy_stages": stages,
            "legacy_elapsed_sec": round(time.monotonic() - start, 2),
            "legacy_run_dir": str(run_root),
            "vr_geometry_fingerprint": vr.geometry_fingerprint,
        })
        (OUT / "recordings.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(f"[{speaker}/{recording}] VR {len(vr.clips)} clips; ASR+MFA {asr_mfa['clip_count']} clips", flush=True)
    pooled = {}
    for method in ("vr", "asr_mfa"):
        sums = {"eligible_target_speech_sec": 0.0, "retained_target_speech_sec": 0.0,
                "clip_count": 0, "unique_cutpoint_count": 0, "inside_phone_count": 0,
                "depth_gt_20ms_count": 0, "depth_gt_50ms_count": 0,
                "depth_gt_100ms_count": 0, "final_edge_inside_phone_count": 0,
                "final_edge_count": 0}
        for row in rows:
            score = row[method]["metrics"]
            unique, edge = score["unique_cut_safety"], score["final_edge_safety"]
            sums["eligible_target_speech_sec"] += score["eligible_target_speech_sec"]
            sums["retained_target_speech_sec"] += score["retained_target_speech_sec"]
            sums["clip_count"] += score["clip_count"]
            for key in ("unique_cutpoint_count", "inside_phone_count", "depth_gt_20ms_count",
                        "depth_gt_50ms_count", "depth_gt_100ms_count"):
                sums[key] += unique[key]
            sums["final_edge_inside_phone_count"] += edge["inside_phone_count"]
            sums["final_edge_count"] += edge["edge_occurrence_count"]
        sums["speech_coverage"] = sums["retained_target_speech_sec"] / sums["eligible_target_speech_sec"]
        sums["inside_phone_rate"] = sums["inside_phone_count"] / sums["unique_cutpoint_count"]
        sums["gt_20ms_rate"] = sums["depth_gt_20ms_count"] / sums["unique_cutpoint_count"]
        sums["gt_50ms_rate"] = sums["depth_gt_50ms_count"] / sums["unique_cutpoint_count"]
        sums["gt_100ms_rate"] = sums["depth_gt_100ms_count"] / sums["unique_cutpoint_count"]
        sums["final_edge_inside_phone_rate"] = sums["final_edge_inside_phone_count"] / sums["final_edge_count"]
        pooled[method] = sums
    report = {
        "subset": SUBSET,
        "selection_rule": "Frozen Phase-4 Buckeye subset, selected before this comparison; one accepted recording each from s01-s04.",
        "reference_data_used_by_slicers": False,
        "metrics": "Both methods rescored by the same Phase-1 referee; ASR+MFA word alignments come from ASR output and MFA, not Buckeye labels.",
        "pooled": pooled,
        "recordings": rows,
    }
    (OUT / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"output": str(OUT / "comparison.json"), "recordings": len(rows)}, indent=2))


if __name__ == "__main__":
    main()
