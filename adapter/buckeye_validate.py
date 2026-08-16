"""Phase 4/5/6/7 Buckeye validation loop. Not a second benchmark engine."""

from __future__ import annotations

import csv
import json
import multiprocessing
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from adapter.buckeye_loader import (
    SUBSET_SELECTION_RULE,
    VALIDATION_SUBSET,
    BuckeyePaths,
    LoadedRecording,
    cohort_identity_fingerprint,
    load_recording,
)
from adapter.buckeye_metrics import (
    assess_qualitative_shape,
    compare_to_historical,
    comparison_table,
    delta_vs_a_table,
    evaluation_signature,
    four_way_comparison_table,
    geometry_comparison_table,
    interpret_pareto,
    metrics_from_evaluation,
    paired_recording_comparison,
    pool_metric_dicts,
)
from adapter.canonical_executor import execute_canonical_diagnosed, execute_shared_geometry_family
from adapter.canonical_smoke import (
    GeometryRun,
    assert_independent_state,
    assert_order_independence,
)
from adapter.config import CURRENT_A, PROPER_D, GeometryConfig, rms_policy_payload
from adapter.convert import to_slicer_result
from adapter.diagnostics import (
    ExecutionDiagnostics,
    geometry_fingerprint,
    geometry_sensitive_signals_differ,
    policy_fingerprint,
    same_contender_signatures,
    same_geometry_signatures,
)
from adapter.types import SlicerRequest
from referee import evaluate
from referee.evaluate import DEPTH_GT_20MS, DEPTH_GT_50MS, DEPTH_GT_100MS, _depth_gt_threshold_ms, _leak_depth_inside_phone
from referee.types import EvaluationResult, SlicerResult


LoadFn = Callable[[str, str], LoadedRecording]
RunFn = Callable[[LoadedRecording, GeometryConfig], GeometryRun]
EvalFn = Callable[[LoadedRecording, SlicerResult], EvaluationResult]

_OUTPUT_NAMES = (
    "manifest.json",
    "summary.json",
    "recordings.csv",
    "unsafe_cutpoints.csv",
    "determinism_checks.json",
)
PUBLIC_CUTPOINT_POPULATION = "selected_schedule"


def geometry_config_payload(config: GeometryConfig) -> dict[str, object]:
    return {
        "name": config.name,
        "window_samples": int(config.window_samples),
        "hop_samples": int(config.hop_samples),
        "offsets": [int(value) for value in config.offsets],
        "sample_rate_hz": int(config.sample_rate_hz),
        "vad_backend": str(config.vad_backend),
        "vad_threshold": float(config.vad_threshold),
        "vad_min_run_ms": float(config.vad_min_run_ms),
        "percentile_rms_percentile": float(config.percentile_rms_percentile),
        "percentile_rms_margin_db": float(config.percentile_rms_margin_db),
        "min_clip_sec": float(config.min_clip_sec),
        "preferred_min_sec": float(config.preferred_min_sec),
        "preferred_max_sec": float(config.preferred_max_sec),
        "target_clip_sec": float(config.target_clip_sec),
        "max_clip_sec": float(config.max_clip_sec),
        "min_quiet_run_ms": (
            None if config.min_quiet_run_ms is None else float(config.min_quiet_run_ms)
        ),
        "scoring": None if not config.scoring else str(config.scoring),
        "rms_policy": None
        if config.rms_policy is None
        else rms_policy_payload(config.rms_policy),
    }


def git_head(root: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_geometry_isolated(loaded: LoadedRecording, config: GeometryConfig) -> GeometryRun:
    """Run A or D with audio + buffers + config only. No annotations."""
    request = SlicerRequest(
        recording_id=loaded.recording_id,
        audio_path=loaded.audio_path,
        sample_rate_hz=loaded.sample_rate_hz,
        buffers=loaded.buffers,
        config=config,
    )
    names = {f.name for f in fields(request)}
    if names != {"recording_id", "audio_path", "sample_rate_hz", "buffers", "config"}:
        raise RuntimeError(f"slicer request leaked unexpected fields: {sorted(names)}")
    for forbidden in ("phones", "words", "uncertainty_intervals", "reference", "annotations"):
        if hasattr(request, forbidden):
            raise RuntimeError(f"slicer request carries annotation field {forbidden!r}")
    execution = execute_canonical_diagnosed(request)
    result = to_slicer_result(execution.raw)
    if {f.name for f in fields(result)} != {"cutpoints", "clips"}:
        raise RuntimeError("public result is not a neutral SlicerResult")
    for clip in result.clips:
        if "|" in clip.buffer_id:
            raise RuntimeError(f"cross-buffer clip buffer_id: {clip.buffer_id}")
    return GeometryRun(result=result, diagnostics=execution.diagnostics)


def _slicer_request(loaded: LoadedRecording, config: GeometryConfig) -> SlicerRequest:
    return SlicerRequest(
        recording_id=loaded.recording_id,
        audio_path=loaded.audio_path,
        sample_rate_hz=loaded.sample_rate_hz,
        buffers=loaded.buffers,
        config=config,
    )


def execute_recording_contenders(
    loaded: LoadedRecording,
    contenders: tuple[GeometryConfig, ...],
) -> dict[str, GeometryRun]:
    """Run controls independently; share one VAD bundle per RMS geometry family."""
    groups: list[list[GeometryConfig]] = []
    family_index: dict[str, int] = {}
    for config in contenders:
        fingerprint = geometry_fingerprint(config)
        if config.rms_policy is None:
            groups.append([config])
            continue
        if fingerprint in family_index:
            groups[family_index[fingerprint]].append(config)
            continue
        family_index[fingerprint] = len(groups)
        groups.append([config])
    runs: dict[str, GeometryRun] = {}
    for group in groups:
        if len(group) == 1 and group[0].rms_policy is None:
            runs[group[0].name] = run_geometry_isolated(loaded, group[0])
            continue
        executions = execute_shared_geometry_family(
            [_slicer_request(loaded, config) for config in group]
        )
        vad_total = sum(item.diagnostics.vad_compute_count for item in executions.values())
        if vad_total != 1:
            raise RuntimeError(
                f"{loaded.recording_id} O25_4-family VAD computation count {vad_total} != 1"
            )
        bundle_ids = {item.diagnostics.feature_bundle_id for item in executions.values()}
        if len(bundle_ids) != 1:
            raise RuntimeError(
                f"{loaded.recording_id} RMS family used multiple feature bundles: {bundle_ids}"
            )
        for config in group:
            execution = executions[config.name]
            result = to_slicer_result(execution.raw)
            if {f.name for f in fields(result)} != {"cutpoints", "clips"}:
                raise RuntimeError("public result is not a neutral SlicerResult")
            runs[config.name] = GeometryRun(result=result, diagnostics=execution.diagnostics)
    missing = [config.name for config in contenders if config.name not in runs]
    if missing:
        raise RuntimeError(f"missing contender runs: {missing}")
    return runs


def evaluate_loaded(loaded: LoadedRecording, result: SlicerResult) -> EvaluationResult:
    return evaluate(loaded.reference, result)


def _recording_row(
    *,
    speaker_id: str,
    recording_id: str,
    geometry: str,
    score: EvaluationResult,
    diagnostics: ExecutionDiagnostics,
    elapsed_sec: float | None = None,
) -> dict[str, object]:
    metrics = metrics_from_evaluation(score)
    return {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "geometry": geometry,
        **metrics.to_dict(),
        "geometry_fingerprint": diagnostics.geometry_fingerprint,
        "policy_fingerprint": diagnostics.policy_fingerprint,
        "vad_observation_count": diagnostics.vad_observation_count,
        "vad_timestamp_sha256": diagnostics.vad_timestamp_sha256,
        "vad_probability_sha256": diagnostics.vad_probability_sha256,
        "candidate_cutpoint_sha256": diagnostics.candidate_cutpoint_sha256,
        "selected_cutpoint_sha256": diagnostics.selected_cutpoint_sha256,
        "selected_clip_sha256": diagnostics.selected_clip_sha256,
        "elapsed_sec": elapsed_sec,
        "workdir": diagnostics.workdir,
        "vad_compute_count": diagnostics.vad_compute_count,
        "feature_bundle_id": diagnostics.feature_bundle_id,
        "vad_compute_sec": diagnostics.vad_compute_sec,
        "policy_eval_sec": diagnostics.policy_eval_sec,
    }


def _unsafe_cut_rows(
    *,
    geometry: str,
    loaded: LoadedRecording,
    result: SlicerResult,
) -> list[dict[str, object]]:
    phones_by_buffer: dict[tuple[str, str], list] = {}
    for phone in loaded.reference.phones:
        phones_by_buffer.setdefault((phone.recording_id, phone.buffer_id), []).append(phone)
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, float]] = set()
    for cut in result.cutpoints:
        key = (cut.recording_id, cut.buffer_id, cut.time_sec)
        if key in seen:
            continue
        seen.add(key)
        depth = _leak_depth_inside_phone(
            cut.time_sec, phones_by_buffer.get((cut.recording_id, cut.buffer_id), [])
        )
        if depth is None:
            continue
        depth_ms = depth * 1000.0
        rows.append(
            {
                "geometry": geometry,
                "speaker_id": loaded.speaker_id,
                "recording_id": cut.recording_id,
                "buffer_id": cut.buffer_id,
                "time_sec": cut.time_sec,
                "depth_ms": round(depth_ms, 6),
                "gt_20ms": _depth_gt_threshold_ms(depth, DEPTH_GT_20MS),
                "gt_50ms": _depth_gt_threshold_ms(depth, DEPTH_GT_50MS),
                "gt_100ms": _depth_gt_threshold_ms(depth, DEPTH_GT_100MS),
            }
        )
    return rows


def assert_order_independent_scores(
    score_ad: EvaluationResult,
    score_da: EvaluationResult,
    *,
    geometry_name: str,
) -> None:
    if evaluation_signature(score_ad) != evaluation_signature(score_da):
        raise RuntimeError(
            f"{geometry_name} evaluator metrics changed across A→D vs D→A order: "
            f"{evaluation_signature(score_ad)!r} vs {evaluation_signature(score_da)!r}"
        )


def completed_recording_pairs(
    rows: list[dict[str, object]],
    *,
    expected_a_fingerprint: str | None = None,
    expected_d_fingerprint: str | None = None,
    expected_fingerprints: dict[str, str] | None = None,
) -> dict[tuple[str, str], dict[str, dict[str, object]]]:
    if expected_fingerprints is None:
        if expected_a_fingerprint is None or expected_d_fingerprint is None:
            raise RuntimeError("resume expected fingerprints are missing")
        expected_fingerprints = {
            "current_A": expected_a_fingerprint,
            "proper_D": expected_d_fingerprint,
        }
    expected_names = set(expected_fingerprints)
    grouped: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]))
        geometry = str(row["geometry"])
        grouped.setdefault(key, {})
        if geometry in grouped[key]:
            raise RuntimeError(f"duplicate {geometry} row for {key[0]}/{key[1]}")
        fingerprint = str(row.get("geometry_fingerprint") or "")
        expected = expected_fingerprints.get(geometry)
        if expected is not None and fingerprint != expected:
            raise RuntimeError(
                f"resume geometry fingerprint mismatch for {geometry} on {key[0]}/{key[1]}"
            )
        grouped[key][geometry] = row
    completed: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for key, geos in grouped.items():
        names = set(geos)
        if names != expected_names:
            if expected_names == {"current_A", "proper_D"}:
                raise RuntimeError(
                    f"incomplete A/D pair for {key[0]}/{key[1]}: {sorted(names)}; "
                    "refusing to skip a partial recording"
                )
            raise RuntimeError(
                f"incomplete contender set for {key[0]}/{key[1]}: {sorted(names)}; "
                "refusing to skip a partial recording"
            )
        completed[key] = geos
    return completed


def _unlink_outputs(output_dir: Path) -> None:
    for name in _OUTPUT_NAMES:
        path = output_dir / name
        if path.exists():
            path.unlink()
        tmp = output_dir / (name + ".tmp")
        if tmp.exists():
            tmp.unlink()


def _read_csv_rows(path: Path) -> list[dict[str, object]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_csv_atomic(path: Path, rows: list[dict[str, object]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    _write_csv(tmp, rows)
    tmp.replace(path)


def _hash_field(row: dict[str, object], name: str) -> str:
    return str(row.get(name) or "")


def _count_schedule_diffs_from_a(
    recording_rows: list[dict[str, object]],
    contender_names: tuple[str, ...],
) -> dict[str, int]:
    """Count recordings whose candidate/selected/final hashes differ from A.

    Includes resumed rows. Some recordings may legitimately match A; the
    full-run guard only fails if an A-derived policy matches on every recording.
    """
    differed = {name: 0 for name in contender_names if name != "current_A"}
    if "current_A" not in contender_names:
        return differed
    return _count_schedule_diffs(recording_rows, contender_names, baseline="current_A")


def _count_schedule_diffs(
    recording_rows: list[dict[str, object]],
    contender_names: tuple[str, ...],
    *,
    baseline: str,
) -> dict[str, int]:
    differed = {name: 0 for name in contender_names if name != baseline}
    if baseline not in contender_names:
        return differed
    by_rec: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    for row in recording_rows:
        key = (str(row["speaker_id"]), str(row["recording_id"]))
        by_rec.setdefault(key, {})[str(row["geometry"])] = row
    for geos in by_rec.values():
        base_row = geos.get(baseline)
        if base_row is None:
            continue
        base_cands = _hash_field(base_row, "candidate_cutpoint_sha256")
        base_cuts = _hash_field(base_row, "selected_cutpoint_sha256")
        base_clips = _hash_field(base_row, "selected_clip_sha256")
        for name in differed:
            other = geos.get(name)
            if other is None:
                continue
            if (
                _hash_field(other, "candidate_cutpoint_sha256") != base_cands
                or _hash_field(other, "selected_cutpoint_sha256") != base_cuts
                or _hash_field(other, "selected_clip_sha256") != base_clips
            ):
                differed[name] += 1
    return differed


def _assert_independence(a_run: GeometryRun, d_run: GeometryRun, *, speaker_id: str, recording_id: str) -> None:
    try:
        if a_run.diagnostics.geometry_fingerprint == d_run.diagnostics.geometry_fingerprint:
            raise AssertionError("A and D geometry fingerprints must differ")
        assert_independent_state(a_run.diagnostics, d_run.diagnostics)
        if not geometry_sensitive_signals_differ(a_run.diagnostics, d_run.diagnostics):
            raise AssertionError(
                "A and D geometry-sensitive observation signatures are identical "
                f"(count={a_run.diagnostics.vad_observation_count}, "
                f"ts={a_run.diagnostics.vad_timestamp_sha256}, "
                f"prob={a_run.diagnostics.vad_probability_sha256})"
            )
    except AssertionError as exc:
        raise RuntimeError(
            f"geometry independence failed on {speaker_id}/{recording_id}: {exc}"
        ) from exc


def _assert_requested_geometries_independent(
    runs: dict[str, GeometryRun],
    contenders: tuple[GeometryConfig, ...],
    *,
    speaker_id: str,
    recording_id: str,
) -> None:
    """Abort if any requested geometry is miswired onto another geometry's VAD stream."""
    by_name = {config.name: config for config in contenders}
    missing = [config.name for config in contenders if config.name not in runs]
    if missing:
        raise RuntimeError(
            f"geometry independence failed on {speaker_id}/{recording_id}: "
            f"missing runs {missing}"
        )
    for config in contenders:
        executed = runs[config.name]
        expected = geometry_fingerprint(config)
        observed = executed.diagnostics.geometry_fingerprint
        if observed != expected:
            raise RuntimeError(
                f"geometry independence failed on {speaker_id}/{recording_id}: "
                f"{config.name} diagnostics fingerprint {observed} != config {expected}"
            )
    names = [config.name for config in contenders]
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left_fp = geometry_fingerprint(by_name[left_name])
            right_fp = geometry_fingerprint(by_name[right_name])
            if left_fp == right_fp:
                left_cfg = by_name[left_name]
                right_cfg = by_name[right_name]
                if left_cfg.rms_policy is not None and right_cfg.rms_policy is not None:
                    left = runs[left_name]
                    right = runs[right_name]
                    if (
                        left.diagnostics.vad_observation_count
                        != right.diagnostics.vad_observation_count
                        or left.diagnostics.vad_timestamp_sha256
                        != right.diagnostics.vad_timestamp_sha256
                        or left.diagnostics.vad_probability_sha256
                        != right.diagnostics.vad_probability_sha256
                    ):
                        raise RuntimeError(
                            f"geometry independence failed on {speaker_id}/{recording_id}: "
                            f"{left_name} and {right_name} share geometry fingerprint "
                            f"{left_fp} but VAD observations differ"
                        )
                continue
            left = runs[left_name]
            right = runs[right_name]
            try:
                assert_independent_state(left.diagnostics, right.diagnostics)
            except AssertionError as exc:
                raise RuntimeError(
                    f"geometry independence failed on {speaker_id}/{recording_id}: "
                    f"{left_name} vs {right_name}: {exc}"
                ) from exc
            if not geometry_sensitive_signals_differ(left.diagnostics, right.diagnostics):
                raise RuntimeError(
                    f"geometry independence failed on {speaker_id}/{recording_id}: "
                    f"{left_name} and {right_name} collapsed onto identical VAD "
                    "observations "
                    f"(count={left.diagnostics.vad_observation_count}, "
                    f"ts={left.diagnostics.vad_timestamp_sha256}, "
                    f"prob={left.diagnostics.vad_probability_sha256})"
                )


def _run_one_recording(
    *,
    speaker_id: str,
    recording_id: str,
    contenders: tuple[GeometryConfig, ...],
    load: LoadFn,
    run: RunFn,
    score_fn: EvalFn,
    reuse_shared_geometry: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, float]]:
    loaded = load(speaker_id, recording_id)
    new_rows: list[dict[str, object]] = []
    new_unsafe: list[dict[str, object]] = []
    runs: dict[str, GeometryRun] = {}
    geom_elapsed: dict[str, float] = {}
    rec_started = time.monotonic()
    try:
        if reuse_shared_geometry:
            print(
                "  running "
                + ", ".join(config.name for config in contenders),
                flush=True,
            )
            geom_started = time.monotonic()
            runs = execute_recording_contenders(loaded, contenders)
            family_elapsed = time.monotonic() - geom_started
            for config in contenders:
                executed = runs[config.name]
                score = score_fn(loaded, executed.result)
                vad = float(executed.diagnostics.vad_compute_sec or 0.0)
                pol = float(executed.diagnostics.policy_eval_sec or 0.0)
                elapsed = vad + pol
                geom_elapsed[config.name] = elapsed
                new_rows.append(
                    _recording_row(
                        speaker_id=speaker_id,
                        recording_id=recording_id,
                        geometry=config.name,
                        score=score,
                        diagnostics=executed.diagnostics,
                        elapsed_sec=round(float(elapsed), 6),
                    )
                )
                new_unsafe.extend(
                    _unsafe_cut_rows(geometry=config.name, loaded=loaded, result=executed.result)
                )
        else:
            for config in contenders:
                print(f"  running {config.name}", flush=True)
                geom_started = time.monotonic()
                executed = run(loaded, config)
                score = score_fn(loaded, executed.result)
                elapsed = time.monotonic() - geom_started
                geom_elapsed[config.name] = elapsed
                runs[config.name] = executed
                new_rows.append(
                    _recording_row(
                        speaker_id=speaker_id,
                        recording_id=recording_id,
                        geometry=config.name,
                        score=score,
                        diagnostics=executed.diagnostics,
                        elapsed_sec=round(elapsed, 6),
                    )
                )
                new_unsafe.extend(
                    _unsafe_cut_rows(geometry=config.name, loaded=loaded, result=executed.result)
                )
        _assert_requested_geometries_independent(
            runs,
            contenders,
            speaker_id=speaker_id,
            recording_id=recording_id,
        )
        if reuse_shared_geometry:
            _assert_o25_family_vad_once(runs, contenders, recording_id=recording_id)
    finally:
        del loaded
    geom_elapsed["recording"] = time.monotonic() - rec_started
    return new_rows, new_unsafe, geom_elapsed


def _assert_o25_family_vad_once(
    runs: dict[str, GeometryRun],
    contenders: tuple[GeometryConfig, ...],
    *,
    recording_id: str,
) -> None:
    family = [config for config in contenders if config.rms_policy is not None]
    if not family:
        return
    total = sum(runs[config.name].diagnostics.vad_compute_count for config in family)
    if total != 1:
        raise RuntimeError(
            f"{recording_id} RMS family VAD computation count {total} != 1 "
            f"({[config.name for config in family]})"
        )
    bundle_ids = {runs[config.name].diagnostics.feature_bundle_id for config in family}
    if len(bundle_ids) != 1 or not next(iter(bundle_ids)):
        raise RuntimeError(
            f"{recording_id} RMS family did not share one feature bundle: {bundle_ids}"
        )


def _parallel_recording_job(job: dict[str, object]) -> dict[str, object]:
    """Spawn-worker entry. Each process owns Silero/temp/detector state."""
    speaker_id = str(job["speaker_id"])
    recording_id = str(job["recording_id"])
    contenders = tuple(job["contenders"])  # type: ignore[arg-type]
    paths = job.get("paths")
    load_fn = job.get("load_fn")
    run_fn = job.get("run_fn")
    eval_fn = job.get("eval_fn")
    load: LoadFn
    if load_fn is not None:
        load = load_fn  # type: ignore[assignment]
    else:
        load = lambda speaker, recording: load_recording(
            speaker, recording, paths=paths  # type: ignore[arg-type]
        )
    run = run_fn if run_fn is not None else run_geometry_isolated
    score_fn = eval_fn if eval_fn is not None else evaluate_loaded
    rows, unsafe, elapsed = _run_one_recording(
        speaker_id=speaker_id,
        recording_id=recording_id,
        contenders=contenders,
        load=load,
        run=run,  # type: ignore[arg-type]
        score_fn=score_fn,  # type: ignore[arg-type]
        reuse_shared_geometry=bool(job.get("reuse_shared_geometry")),
    )
    return {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "rows": rows,
        "unsafe": unsafe,
        "elapsed": elapsed,
    }


def _run_order_spot_check(
    *,
    speaker_id: str,
    recording_id: str,
    load: LoadFn,
    run: RunFn,
    score_fn: EvalFn,
) -> dict[str, object]:
    loaded = load(speaker_id, recording_id)
    try:
        a_ad = run(loaded, CURRENT_A)
        d_ad = run(loaded, PROPER_D)
        _assert_independence(a_ad, d_ad, speaker_id=speaker_id, recording_id=recording_id)
        d_da = run(loaded, PROPER_D)
        a_da = run(loaded, CURRENT_A)
        a_ad_score = score_fn(loaded, a_ad.result)
        d_ad_score = score_fn(loaded, d_ad.result)
        d_da_score = score_fn(loaded, d_da.result)
        a_da_score = score_fn(loaded, a_da.result)
        order_runs = {
            "A_in_AD": a_ad,
            "D_in_AD": d_ad,
            "D_in_DA": d_da,
            "A_in_DA": a_da,
        }
        try:
            assert_order_independence(order_runs)
        except AssertionError as exc:
            raise RuntimeError(
                f"order independence failed on {speaker_id}/{recording_id}: {exc}"
            ) from exc
        assert_order_independent_scores(a_ad_score, a_da_score, geometry_name="current_A")
        assert_order_independent_scores(d_ad_score, d_da_score, geometry_name="proper_D")
        if not same_geometry_signatures(a_ad.diagnostics, a_da.diagnostics):
            raise RuntimeError(
                f"A diagnostics changed across execution order on {speaker_id}/{recording_id}"
            )
        if not same_geometry_signatures(d_ad.diagnostics, d_da.diagnostics):
            raise RuntimeError(
                f"D diagnostics changed across execution order on {speaker_id}/{recording_id}"
            )
        return {
            "speaker_id": speaker_id,
            "recording_id": recording_id,
            "passed": True,
            "A": {
                "geometry_fingerprint": a_ad.diagnostics.geometry_fingerprint,
                "vad_observation_count": a_ad.diagnostics.vad_observation_count,
                "vad_timestamp_sha256": a_ad.diagnostics.vad_timestamp_sha256,
                "vad_probability_sha256": a_ad.diagnostics.vad_probability_sha256,
                "candidate_cutpoint_sha256": a_ad.diagnostics.candidate_cutpoint_sha256,
                "selected_clip_sha256": a_ad.diagnostics.selected_clip_sha256,
            },
            "D": {
                "geometry_fingerprint": d_ad.diagnostics.geometry_fingerprint,
                "vad_observation_count": d_ad.diagnostics.vad_observation_count,
                "vad_timestamp_sha256": d_ad.diagnostics.vad_timestamp_sha256,
                "vad_probability_sha256": d_ad.diagnostics.vad_probability_sha256,
                "candidate_cutpoint_sha256": d_ad.diagnostics.candidate_cutpoint_sha256,
                "selected_clip_sha256": d_ad.diagnostics.selected_clip_sha256,
            },
        }
    finally:
        del loaded


def _spot_orders(
    contenders: tuple[GeometryConfig, ...],
) -> tuple[tuple[GeometryConfig, ...], tuple[GeometryConfig, ...]]:
    by_name = {config.name: config for config in contenders}
    if set(by_name) == {"current_A", "proper_D"}:
        return (by_name["current_A"], by_name["proper_D"]), (
            by_name["proper_D"],
            by_name["current_A"],
        )
    if set(by_name) >= {"current_A", "min_quiet_run_64ms", "quiet_run_score", "proper_D"}:
        forward = (
            by_name["current_A"],
            by_name["min_quiet_run_64ms"],
            by_name["quiet_run_score"],
            by_name["proper_D"],
        )
        reverse = (
            by_name["proper_D"],
            by_name["quiet_run_score"],
            by_name["min_quiet_run_64ms"],
            by_name["current_A"],
        )
        return forward, reverse
    return contenders, tuple(reversed(contenders))


def _run_contender_order_spot_check(
    *,
    speaker_id: str,
    recording_id: str,
    load: LoadFn,
    run: RunFn,
    score_fn: EvalFn,
    contenders: tuple[GeometryConfig, ...],
    reuse_shared_geometry: bool = False,
) -> dict[str, object]:
    loaded = load(speaker_id, recording_id)
    try:
        forward, reverse = _spot_orders(contenders)
        print(
            f"  order {speaker_id}/{recording_id}: "
            + " → ".join(cfg.name for cfg in forward),
            flush=True,
        )

        def _runs_for(order: tuple[GeometryConfig, ...]) -> dict[str, GeometryRun]:
            if reuse_shared_geometry:
                return execute_recording_contenders(loaded, order)
            return {config.name: run(loaded, config) for config in order}

        forward_executed = _runs_for(forward)
        forward_runs = {
            name: (executed, score_fn(loaded, executed.result))
            for name, executed in forward_executed.items()
        }
        print(
            f"  order {speaker_id}/{recording_id}: "
            + " → ".join(cfg.name for cfg in reverse),
            flush=True,
        )
        reverse_executed = _runs_for(reverse)
        reverse_runs = {
            name: (executed, score_fn(loaded, executed.result))
            for name, executed in reverse_executed.items()
        }
        _assert_requested_geometries_independent(
            {name: pair[0] for name, pair in forward_runs.items()},
            contenders,
            speaker_id=speaker_id,
            recording_id=recording_id,
        )
        _assert_requested_geometries_independent(
            {name: pair[0] for name, pair in reverse_runs.items()},
            contenders,
            speaker_id=speaker_id,
            recording_id=recording_id,
        )
        if reuse_shared_geometry:
            _assert_o25_family_vad_once(
                {name: pair[0] for name, pair in forward_runs.items()},
                contenders,
                recording_id=recording_id,
            )
            family = [config for config in contenders if config.rms_policy is not None]
            if family:
                forward_ids = {forward_runs[c.name][0].diagnostics.feature_bundle_id for c in family}
                reverse_ids = {reverse_runs[c.name][0].diagnostics.feature_bundle_id for c in family}
                if len(forward_ids) != 1 or len(reverse_ids) != 1:
                    raise RuntimeError(
                        f"{recording_id} policy-order check did not keep a shared O25_4 bundle"
                    )
                hashes = {
                    (
                        pair[0].diagnostics.vad_timestamp_sha256,
                        pair[0].diagnostics.vad_probability_sha256,
                    )
                    for name, pair in forward_runs.items()
                    if name in {c.name for c in family}
                }
                if len(hashes) != 1:
                    raise RuntimeError(
                        f"{recording_id} shared O25_4 VAD hashes diverged across policies"
                    )
        compact: dict[str, object] = {
            "speaker_id": speaker_id,
            "recording_id": recording_id,
            "passed": True,
            "forward_order": [cfg.name for cfg in forward],
            "reverse_order": [cfg.name for cfg in reverse],
        }
        for config in contenders:
            name = config.name
            first_run, first_score = forward_runs[name]
            second_run, second_score = reverse_runs[name]
            if not same_contender_signatures(first_run.diagnostics, second_run.diagnostics):
                raise RuntimeError(
                    f"{name} signatures changed across execution order on "
                    f"{speaker_id}/{recording_id}"
                )
            assert_order_independent_scores(first_score, second_score, geometry_name=name)
            compact[name] = {
                "geometry_fingerprint": first_run.diagnostics.geometry_fingerprint,
                "policy_fingerprint": first_run.diagnostics.policy_fingerprint,
                "vad_observation_count": first_run.diagnostics.vad_observation_count,
                "vad_timestamp_sha256": first_run.diagnostics.vad_timestamp_sha256,
                "vad_probability_sha256": first_run.diagnostics.vad_probability_sha256,
                "candidate_cutpoint_sha256": first_run.diagnostics.candidate_cutpoint_sha256,
                "selected_cutpoint_sha256": first_run.diagnostics.selected_cutpoint_sha256,
                "selected_clip_sha256": first_run.diagnostics.selected_clip_sha256,
            }
        return compact
    finally:
        del loaded


def run_validation(
    *,
    output_dir: Path,
    subset: tuple[tuple[str, str], ...] = VALIDATION_SUBSET,
    paths: BuckeyePaths | None = None,
    load_fn: LoadFn | None = None,
    run_fn: RunFn | None = None,
    eval_fn: EvalFn | None = None,
    order_check_index: int = 0,
    spot_check_ids: Sequence[tuple[str, str]] | None = None,
    resume: bool = False,
    selection_rule: str = SUBSET_SELECTION_RULE,
    cohort_info: dict[str, object] | None = None,
    contenders: tuple[GeometryConfig, ...] | None = None,
    workers: int = 1,
) -> dict[str, object]:
    """Process one recording at a time. Exceptions abort the run."""
    load = load_fn if load_fn is not None else (
        lambda speaker_id, recording_id: load_recording(speaker_id, recording_id, paths=paths)
    )
    run = run_fn if run_fn is not None else run_geometry_isolated
    score_fn = eval_fn if eval_fn is not None else evaluate_loaded
    active_contenders = contenders if contenders is not None else (CURRENT_A, PROPER_D)
    reuse_shared_geometry = run_fn is None
    rms_family = tuple(config.name for config in active_contenders if config.rms_policy is not None)
    contender_names = tuple(config.name for config in active_contenders)
    if len(set(contender_names)) != len(contender_names):
        raise RuntimeError(f"duplicate contender names: {contender_names}")
    if workers < 1:
        raise RuntimeError(f"workers must be >= 1, got {workers}")
    if not subset:
        raise RuntimeError("validation subset is empty")
    subset_set = set(subset)
    if spot_check_ids is None:
        spot_ids = (subset[order_check_index],)
    else:
        spot_ids = tuple(spot_check_ids)
        missing = [item for item in spot_ids if item not in subset_set]
        if missing:
            raise RuntimeError(f"spot-check recordings are not in the cohort: {missing}")
    fingerprint = cohort_identity_fingerprint(subset)
    expected_fingerprints = {config.name: geometry_fingerprint(config) for config in active_contenders}
    expected_policy = {config.name: policy_fingerprint(config) for config in active_contenders}
    active_paths = paths if paths is not None else BuckeyePaths.canonical()
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "summary.json"
    if summary_path.exists():
        summary_path.unlink()

    recordings_path = output_dir / "recordings.csv"
    unsafe_path = output_dir / "unsafe_cutpoints.csv"
    manifest_path = output_dir / "manifest.json"
    completed: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
    unsafe_by_rec: dict[tuple[str, str], list[dict[str, object]]] = {}
    config_payloads = {config.name: geometry_config_payload(config) for config in active_contenders}

    if resume and recordings_path.exists() and recordings_path.stat().st_size > 0:
        if not manifest_path.is_file():
            raise RuntimeError("resume requested but manifest.json is missing")
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing_manifest.get("cohort_fingerprint") != fingerprint:
            raise RuntimeError("resume cohort fingerprint does not match this run")
        if existing_manifest.get("contender_names") != list(contender_names):
            if existing_manifest.get("config_A") != config_payloads.get("current_A") or (
                existing_manifest.get("config_D") != config_payloads.get("proper_D")
            ):
                raise RuntimeError("resume contender configs do not match this run")
            if tuple(existing_manifest.get("contender_names") or ("current_A", "proper_D")) != contender_names:
                raise RuntimeError("resume contender set does not match this run")
        if existing_manifest.get("public_cutpoint_population") != PUBLIC_CUTPOINT_POPULATION:
            raise RuntimeError(
                "resume cutpoint population contract does not match this run "
                f"(expected {PUBLIC_CUTPOINT_POPULATION!r})"
            )
        existing_rows = _read_csv_rows(recordings_path)
        completed = completed_recording_pairs(
            existing_rows,
            expected_fingerprints=expected_fingerprints,
        )
        extra = set(completed) - subset_set
        if extra:
            raise RuntimeError(f"resume CSV contains recordings outside the cohort: {sorted(extra)}")
        for row in _read_csv_rows(unsafe_path):
            key = (str(row["speaker_id"]), str(row["recording_id"]))
            unsafe_by_rec.setdefault(key, []).append(row)
        manifest = existing_manifest
    else:
        if not resume:
            _unlink_outputs(output_dir)
        elif recordings_path.exists() and recordings_path.stat().st_size == 0:
            pass
        manifest = {
            "run_timestamp": datetime.now(timezone.utc).isoformat(),
            "cohort_fingerprint": fingerprint,
            "recording_count": len(subset),
            "selection_rule": selection_rule,
            "subset": [{"speaker_id": s, "recording_id": r} for s, r in subset],
            "contender_names": list(contender_names),
            "contender_configs": config_payloads,
            "config_A": config_payloads.get("current_A") or config_payloads.get("A_O50_8"),
            "config_D": config_payloads.get("proper_D") or config_payloads.get("D_O0_8"),
            "vad_backend": CURRENT_A.vad_backend,
            "public_cutpoint_population": PUBLIC_CUTPOINT_POPULATION,
            "geometry_fingerprints": expected_fingerprints,
            "policy_fingerprints": expected_policy,
            "geometry_fingerprint_A": expected_fingerprints.get("current_A")
            or expected_fingerprints.get("A_O50_8"),
            "geometry_fingerprint_D": expected_fingerprints.get("proper_D")
            or expected_fingerprints.get("D_O0_8"),
            "workers": workers,
            "canonical_paths": {
                "normalized_root": str(active_paths.normalized_root),
                "cohort_root": str(active_paths.cohort_root),
                "cluster_mapping_csv": str(active_paths.cluster_mapping_csv),
                "speaker_cohort_summary_csv": str(
                    active_paths.cohort_root / "speaker_cohort_summary.csv"
                ),
            },
            "git_commit": git_head(output_dir.parent),
            "O25_4_feature_reuse": bool(rms_family) and reuse_shared_geometry,
            "O25_4_vad_computations_per_recording": 1 if rms_family else None,
            "model_backend_identity": CURRENT_A.vad_backend,
            "paths": {
                "normalized_root": str(active_paths.normalized_root),
                "cohort_root": str(active_paths.cohort_root),
            },
        }
        if cohort_info:
            manifest["cohort"] = cohort_info
        _write_json(manifest_path, manifest)

    recording_rows: list[dict[str, object]] = []
    unsafe_rows: list[dict[str, object]] = []
    total = len(subset)
    started = time.monotonic()
    pending = [item for item in subset if item not in completed]
    for key in [item for item in subset if item in completed]:
        index = subset.index(key) + 1
        elapsed = time.monotonic() - started
        print(
            f"[{index}/{total}] {key[0]}/{key[1]}  reused completed row  elapsed={elapsed:.1f}s",
            flush=True,
        )

    def _rows_from_completed(*, require_all: bool) -> None:
        recording_rows.clear()
        unsafe_rows.clear()
        for speaker_id, recording_id in subset:
            key = (speaker_id, recording_id)
            if key not in completed:
                if require_all:
                    raise RuntimeError(f"missing completed rows for {speaker_id}/{recording_id}")
                continue
            for name in contender_names:
                recording_rows.append(completed[key][name])
            unsafe_rows.extend(unsafe_by_rec.get(key, []))

    if pending and workers == 1:
        for speaker_id, recording_id in pending:
            key = (speaker_id, recording_id)
            index = subset.index(key) + 1
            elapsed = time.monotonic() - started
            print(f"[{index}/{total}] {speaker_id}/{recording_id}  elapsed={elapsed:.1f}s", flush=True)
            new_rows, new_unsafe, _elapsed = _run_one_recording(
                speaker_id=speaker_id,
                recording_id=recording_id,
                contenders=active_contenders,
                load=load,
                run=run,
                score_fn=score_fn,
                reuse_shared_geometry=reuse_shared_geometry,
            )
            completed[key] = {str(row["geometry"]): row for row in new_rows}
            unsafe_by_rec[key] = new_unsafe
            _rows_from_completed(require_all=False)
            _write_csv_atomic(recordings_path, recording_rows)
            _write_csv_atomic(unsafe_path, unsafe_rows)
    elif pending:
        print(f"parallel workers={workers} recordings={len(pending)}", flush=True)
        jobs = [
            {
                "speaker_id": speaker_id,
                "recording_id": recording_id,
                "contenders": active_contenders,
                "paths": active_paths if load_fn is None else None,
                "load_fn": load_fn,
                "run_fn": run_fn,
                "eval_fn": eval_fn,
                "reuse_shared_geometry": reuse_shared_geometry,
            }
            for speaker_id, recording_id in pending
        ]
        ctx = multiprocessing.get_context(
            "spawn" if load_fn is None and run_fn is None and eval_fn is None else "fork"
        )
        with ctx.Pool(processes=workers) as pool:
            received: set[tuple[str, str]] = set()
            for item in pool.imap_unordered(_parallel_recording_job, jobs):
                speaker_id = str(item["speaker_id"])
                recording_id = str(item["recording_id"])
                key = (speaker_id, recording_id)
                if key in received:
                    raise RuntimeError(f"duplicate parallel result for {speaker_id}/{recording_id}")
                received.add(key)
                new_rows = list(item["rows"])  # type: ignore[arg-type]
                new_unsafe = list(item["unsafe"])  # type: ignore[arg-type]
                completed[key] = {str(row["geometry"]): row for row in new_rows}
                unsafe_by_rec[key] = new_unsafe
                print(
                    f"  committed {speaker_id}/{recording_id}  "
                    f"done={len(received)}/{len(pending)}",
                    flush=True,
                )
                _rows_from_completed(require_all=False)
                _write_csv_atomic(recordings_path, recording_rows)
                _write_csv_atomic(unsafe_path, unsafe_rows)
        if received != set(pending):
            raise RuntimeError(
                f"parallel worker results do not match pending recordings: "
                f"got {sorted(received)} expected {sorted(pending)}"
            )
    else:
        _rows_from_completed(require_all=True)
    _rows_from_completed(require_all=True)

    output_ids = [
        (str(row["speaker_id"]), str(row["recording_id"]))
        for row in recording_rows
        if str(row["geometry"]) == contender_names[0]
    ]
    if tuple(output_ids) != subset:
        raise RuntimeError(
            f"output cohort {len(output_ids)} recordings != derived cohort {len(subset)}"
        )
    pooled_by_name: dict[str, object] = {}
    for name in contender_names:
        rows = [row for row in recording_rows if str(row["geometry"]) == name]
        if len(rows) != len(subset):
            raise RuntimeError(f"{name} row count {len(rows)} != cohort {len(subset)}")
        pooled_by_name[name] = pool_metric_dicts(rows)

    differed_from_a = _count_schedule_diffs_from_a(recording_rows, contender_names)

    print("determinism spot checks", flush=True)
    determinism_checks = [
        _run_contender_order_spot_check(
            speaker_id=speaker_id,
            recording_id=recording_id,
            load=load,
            run=run,
            score_fn=score_fn,
            contenders=active_contenders,
            reuse_shared_geometry=reuse_shared_geometry,
        )
        for speaker_id, recording_id in spot_ids
    ]

    pooled_a = pooled_by_name.get("current_A") or pooled_by_name.get("A_O50_8")
    pooled_d = pooled_by_name.get("proper_D") or pooled_by_name.get("D_O0_8")
    table = None
    qualitative = None
    paired = None
    historical = None
    if pooled_a is not None and pooled_d is not None:
        table = comparison_table(pooled_a, pooled_d)
        qualitative = assess_qualitative_shape(pooled_a, pooled_d)
        if "current_A" in contender_names and "proper_D" in contender_names:
            paired = paired_recording_comparison(recording_rows)
            historical = compare_to_historical(
                recording_count=len(subset),
                pooled_a=pooled_a,
                pooled_d=pooled_d,
                paired=paired,
                qualitative=qualitative,
            )
        elif "A_O50_8" in contender_names and "D_O0_8" in contender_names:
            paired = paired_recording_comparison(
                recording_rows, left="A_O50_8", right="D_O0_8"
            )
    four_way = None
    deltas = None
    paired_vs_a = None
    pareto = None
    if set(contender_names) >= {"current_A", "proper_D", "min_quiet_run_64ms", "quiet_run_score"}:
        four_way = four_way_comparison_table(pooled_by_name)  # type: ignore[arg-type]
        deltas = delta_vs_a_table(pooled_by_name)  # type: ignore[arg-type]
        paired_vs_a = {
            name: paired_recording_comparison(recording_rows, left="current_A", right=name)
            for name in ("proper_D", "min_quiet_run_64ms", "quiet_run_score")
        }
        pareto = [
            interpret_pareto(
                name=name,
                contender=pooled_by_name[name],  # type: ignore[arg-type]
                pooled_a=pooled_a,  # type: ignore[arg-type]
                pooled_d=pooled_d,  # type: ignore[arg-type]
            )
            for name in ("min_quiet_run_64ms", "quiet_run_score")
        ]
        a_derived = ("min_quiet_run_64ms", "quiet_run_score")
        silent = [name for name in a_derived if differed_from_a.get(name, 0) == 0]
        if silent:
            raise RuntimeError(
                "A-derived policies were identical to A on every recording: "
                f"{silent}. Policy config is probably not wired."
            )
    differed_from_current = None
    if "O25_4_CURRENT" in contender_names and len(rms_family) > 1:
        differed_from_current = _count_schedule_diffs(
            recording_rows, contender_names, baseline="O25_4_CURRENT"
        )
        kind_by_name = {
            config.name: None if config.rms_policy is None else config.rms_policy.kind
            for config in active_contenders
        }
        # A conservative veto may keep every CURRENT candidate. Score-mutating
        # policies that never change the packer output are still treated as unwired.
        silent_rms = [
            name
            for name in rms_family
            if name != "O25_4_CURRENT"
            and differed_from_current.get(name, 0) == 0
            and kind_by_name.get(name) != "weak_valley_veto"
        ]
        if silent_rms:
            raise RuntimeError(
                "O25_4 RMS variants were identical to O25_4_CURRENT on every recording: "
                f"{silent_rms}. Policy config is probably not wired."
            )
    geometry_table = None
    geometry_deltas = None
    paired_vs_baseline = None
    paired_vs_o25_current = None
    baseline_name = "A_O50_8" if "A_O50_8" in contender_names else None
    if len(contender_names) >= 2:
        geometry_table = geometry_comparison_table(pooled_by_name, contender_names)  # type: ignore[arg-type]
    if baseline_name is not None and len(contender_names) >= 2:
        geometry_deltas = delta_vs_a_table(
            pooled_by_name,  # type: ignore[arg-type]
            baseline=baseline_name,
            contender_names=list(contender_names),
        )
        paired_vs_baseline = {
            name: paired_recording_comparison(recording_rows, left=baseline_name, right=name)
            for name in contender_names
            if name != baseline_name
        }
    if "O25_4_CURRENT" in contender_names:
        paired_vs_o25_current = {
            name: paired_recording_comparison(
                recording_rows, left="O25_4_CURRENT", right=name
            )
            for name in contender_names
            if name != "O25_4_CURRENT"
        }
    elapsed_sec = time.monotonic() - started
    elapsed_by_geometry = {
        name: sum(
            float(row["elapsed_sec"])
            for row in recording_rows
            if str(row["geometry"]) == name and row.get("elapsed_sec") not in (None, "")
        )
        for name in contender_names
    }

    has_ad = (
        ("current_A" in contender_names and "proper_D" in contender_names)
        or ("A_O50_8" in contender_names and "D_O0_8" in contender_names)
    )

    summary = {
        "subset": [{"speaker_id": s, "recording_id": r} for s, r in subset],
        "selection_rule": selection_rule,
        "cohort_fingerprint": fingerprint,
        "recording_count": len(subset),
        "contender_names": list(contender_names),
        "elapsed_sec": elapsed_sec,
        "elapsed_by_geometry": elapsed_by_geometry,
        "workers": workers,
        "order_independence_recording": determinism_checks[0],
        "determinism_checks": determinism_checks,
        "ad_observation_signatures_differed": True,
        "ad_independence_guard": "passed on every recording" if has_ad else "n/a",
        "schedule_differed_from_A_recordings": differed_from_a,
        "pooled": {name: metrics.to_dict() for name, metrics in pooled_by_name.items()},
        "pooled_A": None if pooled_a is None else pooled_a.to_dict(),
        "pooled_D": None if pooled_d is None else pooled_d.to_dict(),
        "paired_coverage": paired,
        "paired_vs_A": paired_vs_a,
        "comparison": table,
        "four_way": four_way,
        "geometry_table": geometry_table,
        "geometry_delta_vs_A": geometry_deltas,
        "paired_vs_baseline": paired_vs_baseline,
        "paired_vs_o25_current": paired_vs_o25_current,
        "delta_vs_A": deltas,
        "pareto": pareto,
        "qualitative": qualitative,
        "historical_comparison": historical,
        "O25_4_feature_reuse": bool(rms_family) and reuse_shared_geometry,
        "O25_4_vad_computations_per_recording": 1 if rms_family and reuse_shared_geometry else None,
        "schedule_diffs_vs_o25_current": differed_from_current,
        "runtime": {
            "whole_run_sec": elapsed_sec,
            "O25_4_vad_compute_sec": sum(
                float(row["vad_compute_sec"])
                for row in recording_rows
                if str(row["geometry"]) in rms_family
                and row.get("vad_compute_sec") not in (None, "")
            ),
            "O25_4_policy_eval_sec": sum(
                float(row["policy_eval_sec"])
                for row in recording_rows
                if str(row["geometry"]) in rms_family
                and row.get("policy_eval_sec") not in (None, "")
            ),
            "A_O50_8_sec": sum(
                float(row["vad_compute_sec"] or 0) + float(row["policy_eval_sec"] or 0)
                for row in recording_rows
                if str(row["geometry"]) == "A_O50_8"
            ),
            "O0_4_sec": sum(
                float(row["vad_compute_sec"] or 0) + float(row["policy_eval_sec"] or 0)
                for row in recording_rows
                if str(row["geometry"]) == "O0_4"
            ),
            "O0_2_sec": sum(
                float(row["vad_compute_sec"] or 0) + float(row["policy_eval_sec"] or 0)
                for row in recording_rows
                if str(row["geometry"]) == "O0_2"
            ),
        },
        "passed": True,
    }
    _write_json(output_dir / "manifest.json", manifest)
    _write_json(output_dir / "summary.json", summary)
    _write_csv_atomic(recordings_path, recording_rows)
    _write_csv_atomic(unsafe_path, unsafe_rows)
    _write_json(output_dir / "determinism_checks.json", determinism_checks)
    return summary
