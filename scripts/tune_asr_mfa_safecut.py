"""Tune historical ASR+MFA SafeCut thresholds on cached four-recording alignments."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval_runs/slicer_head_to_head_2026-10-07"
RUNNER_PATH = ROOT / "scripts/run_asr_mfa_vr_comparison.py"
NEMO_ROOT = Path("/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_nemo_batch_v1/runs")
SOURCE_RUN_NAME = "2026-07-17_000000_buckeye_{speaker}_repaired_slicer_dry_run_production_safecut_optimal"

# Sweep symmetric edge guards and minimum usable gap. The old setting is included.
SYMMETRIC_SETTINGS = [(guard, guard, gap) for guard in (0, 10, 20, 30) for gap in (0, 20, 40, 60, 80)]
ASYMMETRIC_SETTINGS = [
    (left, right, gap)
    for left, right in ((0, 30), (10, 30), (30, 10), (30, 0))
    for gap in (20, 40, 60)
]
FIXED_100MS_SETTINGS = [
    (left, right, 100 - left - right)
    for left in (0, 10, 20, 30, 40)
    for right in (0, 10, 20, 30, 40)
    if left + right <= 100
]


def load_runner():
    overlay_package = OUT / "legacy_worker_overlay/speechcraft_dataset"
    shutil.copy2(ROOT / "workers/dataset/speechcraft_dataset/vr_slicer.py", overlay_package / "vr_slicer.py")
    sys.path.insert(0, str(OUT / "legacy_worker_overlay"))
    spec = importlib.util.spec_from_file_location("comparison_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.install_legacy_worker_overlay()
    return module


def hardlink_tree(src: Path, dst: Path):
    shutil.copytree(src, dst, copy_function=os.link)


def main():
    runner = load_runner()
    bench = runner.bench
    from speechcraft_dataset.safecut import generate_safe_cutpoint_diagnostics

    fixed100 = "--fixed100" in sys.argv
    asymmetric = "--asymmetric" in sys.argv
    settings = FIXED_100MS_SETTINGS if fixed100 else ASYMMETRIC_SETTINGS if asymmetric else SYMMETRIC_SETTINGS
    tuning_name = "safecut_tuning_fixed100" if fixed100 else "safecut_tuning_asymmetric" if asymmetric else "safecut_tuning"
    tuning_root = OUT / tuning_name
    tuning_root.mkdir(parents=True, exist_ok=True)
    results = []
    for left_guard_ms, right_guard_ms, usable_gap_ms in settings:
        setting = f"left{left_guard_ms}_right{right_guard_ms}_gap{usable_gap_ms}"
        print(f"=== {setting} ===", flush=True)
        recording_results = []
        for speaker, recording in runner.SUBSET:
            source_run = OUT / "asr_mfa_runs" / speaker / SOURCE_RUN_NAME.format(speaker=speaker) / "speechcraft_run"
            variant_out = tuning_root / setting / speaker
            output_run = variant_out / SOURCE_RUN_NAME.format(speaker=speaker) / "speechcraft_run"
            if output_run.exists():
                shutil.rmtree(output_run.parent)
            output_run.parent.mkdir(parents=True, exist_ok=True)
            hardlink_tree(source_run, output_run)

            config_path = output_run / "config.json"
            config_path.unlink()
            shutil.copy2(source_run / "config.json", config_path)
            config = json.loads(config_path.read_text())
            config["cutpoint_left_word_edge_guard_ms"] = left_guard_ms
            config["cutpoint_right_word_edge_guard_ms"] = right_guard_ms
            config["cutpoint_min_gap_ms"] = usable_gap_ms
            config_path.write_text(json.dumps(config, indent=2) + "\n")
            for name in ("safe_cutpoints.jsonl", "rejected_cutpoint_candidates.jsonl", "safe_cutpoint_summary.json"):
                target = output_run / "artifacts" / name
                target.unlink(missing_ok=True)
                shutil.copy2(source_run / "artifacts" / name, target)
            safe_summary = generate_safe_cutpoint_diagnostics(output_run, config)

            eval_run = NEMO_ROOT / speaker / recording
            paths = runner.BuckeyePaths.canonical()
            loaded = runner.load_recording(speaker, recording, paths=paths)
            result_root = bench.run_repaired_production_dry_run(
                eval_run=eval_run,
                normalized_speaker_dir=paths.normalized_root / speaker,
                cluster_mapping_csv=paths.cluster_mapping_csv,
                out_root=variant_out,
                shared_cache_root=OUT / "shared_cache",
                write_heavy_tables=True,
            )
            score = runner.score_saved_tables(loaded.reference, result_root / "tables")
            metrics = score["metrics"]
            recording_results.append({
                "speaker_id": speaker,
                "recording_id": recording,
                "metrics": metrics,
                "cut_count": score["cut_count"],
                "clip_count": score["clip_count"],
                "safe_cutpoint_count": safe_summary["accepted_cutpoints"],
                "safe_cutpoint_rejections": safe_summary["rejection_reason_counts"],
                "result_root": str(result_root),
            })
            print(f"  {recording}: cuts={score['cut_count']} clips={score['clip_count']} "
                  f"coverage={metrics['speech_coverage']:.3f} inside={metrics['unique_cut_safety']['inside_phone_rate']:.3f}", flush=True)

        eligible = sum(r["metrics"]["eligible_target_speech_sec"] for r in recording_results)
        retained = sum(r["metrics"]["retained_target_speech_sec"] for r in recording_results)
        cut_count = sum(r["metrics"]["unique_cut_safety"]["unique_cutpoint_count"] for r in recording_results)
        inside = sum(r["metrics"]["unique_cut_safety"]["inside_phone_count"] for r in recording_results)
        depth50 = sum(r["metrics"]["unique_cut_safety"]["depth_gt_50ms_count"] for r in recording_results)
        depth100 = sum(r["metrics"]["unique_cut_safety"]["depth_gt_100ms_count"] for r in recording_results)
        results.append({
            "setting": setting,
            "left_guard_ms": left_guard_ms,
            "right_guard_ms": right_guard_ms,
            "min_usable_gap_ms": usable_gap_ms,
            "pooled": {
                "speech_coverage": retained / eligible if eligible else 0,
                "eligible_target_speech_sec": eligible,
                "retained_target_speech_sec": retained,
                "inside_phone_rate": inside / cut_count if cut_count else 0,
                "inside_phone_count": inside,
                "unique_cutpoint_count": cut_count,
                "depth_gt_50ms_count": depth50,
                "depth_gt_100ms_count": depth100,
                "clip_count": sum(r["clip_count"] for r in recording_results),
            },
            "recordings": recording_results,
        })
        (tuning_root / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    results.sort(key=lambda r: (r["pooled"]["speech_coverage"], -r["pooled"]["inside_phone_rate"]), reverse=True)
    report = {
        "method": "Cached ASR transcripts, MFA aligned words and alignment QC reused; SafeCut candidates regenerated per setting; same recording scopes, common packer, and Phase-1 referee.",
        "settings_tested": len(settings),
        "subset": runner.SUBSET,
        "ranked_by": "coverage then lower inside-phone rate",
        "results": results,
    }
    (tuning_root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps([{"setting": r["setting"], **r["pooled"]} for r in results], indent=2))


if __name__ == "__main__":
    main()
