"""Phase 9 external vs frozen O0_4 validation loop."""

from __future__ import annotations

import multiprocessing
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from adapter.buckeye_loader import (
    PHASE5_COHORT_FINGERPRINT,
    PHASE7_SMOKE_SELECTION_RULE,
    PHASE7_SMOKE_SUBSET,
    BuckeyePaths,
    LoadedRecording,
    cohort_identity_fingerprint,
    derive_full_cohort,
    load_recording,
)
from adapter.buckeye_metrics import (
    format_geometry_table,
    geometry_comparison_table,
    metrics_from_evaluation,
    pool_metric_dicts,
)
from adapter.buckeye_validate import (
    PUBLIC_CUTPOINT_POPULATION,
    _recording_row,
    _unsafe_cut_rows,
    _unlink_outputs,
    _write_csv_atomic,
    _write_json,
    assert_order_independent_scores,
    evaluate_loaded,
    git_head,
    run_geometry_isolated,
)
from adapter.canonical_executor import _load_audio_float32
from adapter.canonical_smoke import GeometryRun
from adapter.config import O0_4
from adapter.diagnostics import geometry_fingerprint, same_contender_signatures
from adapter.external_baselines import (
    ADAPTER_VERSION,
    BLOG_TABLE_ROWS,
    OPENVPI_REVISION,
    PARAMETER_POLICY,
    PHASE9_EXTERNAL_SPECS,
    PHASE9_SMOKE_SYSTEMS,
    RVC_DECISION,
    ExternalBaselineSpec,
    require_frozen_o0_4,
    require_openvpi_vendor,
)
from adapter.external_detect import ExternalAudioRequest
from adapter.external_execute import ExternalRun, execute_external
from adapter.external_ffmpeg import ffmpeg_version
from referee.evaluate import evaluate


def _library_versions() -> dict[str, str]:
    import importlib.metadata as metadata

    import librosa
    import numpy
    import soundfile

    try:
        pydub_version = metadata.version("pydub")
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError("pydub is missing from the dataset environment") from exc
    vendor = require_openvpi_vendor()
    return {
        "librosa": librosa.__version__,
        "numpy": numpy.__version__,
        "soundfile": soundfile.__version__,
        "pydub": pydub_version,
        "ffmpeg": ffmpeg_version(),
        "openvpi_revision": OPENVPI_REVISION,
        "openvpi_slicer2_sha256": vendor["sha256"],
        "adapter_version": ADAPTER_VERSION,
    }


def _spec_payload(spec: ExternalBaselineSpec) -> dict[str, object]:
    return {
        **spec.canonical_payload(),
        "fingerprint": spec.fingerprint(),
        "detector_behavior": spec.params,
        "adapter_behavior": {
            "buffer_local": True,
            "candidate_rule": spec.candidate_rule,
            "native_legal_filter": spec.mode == "native",
        },
        "common_packer_behavior": spec.packing,
    }


def _run_o0_4(loaded: LoadedRecording) -> GeometryRun:
    return run_geometry_isolated(loaded, require_frozen_o0_4())


def _run_external(loaded: LoadedRecording, spec: ExternalBaselineSpec, audio: object, rate: int) -> ExternalRun:
    if int(rate) != int(loaded.sample_rate_hz):
        raise RuntimeError(
            f"{loaded.recording_id} decoded rate {rate} != {loaded.sample_rate_hz}"
        )
    request = ExternalAudioRequest(
        recording_id=loaded.recording_id,
        sample_rate_hz=int(loaded.sample_rate_hz),
        buffers=loaded.buffers,
        audio=audio,
        spec=spec,
    )
    request.assert_firewall()
    return execute_external(request)


def _systems_in_order(names: Sequence[str]) -> tuple[str, ...]:
    known = set(PHASE9_SMOKE_SYSTEMS)
    missing = [name for name in names if name not in known]
    if missing:
        raise RuntimeError(f"unknown Phase 9 systems {missing}")
    return tuple(names)


def _legality_row(
    *,
    speaker_id: str,
    recording_id: str,
    spec: ExternalBaselineSpec,
    run: ExternalRun,
    elapsed_sec: float,
) -> dict[str, object]:
    payload = run.legality.to_dict()
    payload.update(
        {
            "speaker_id": speaker_id,
            "recording_id": recording_id,
            "system": spec.name,
            "mode": spec.mode,
            "elapsed_sec": round(elapsed_sec, 6),
            "detector_runtime_sec": run.diagnostics.vad_compute_sec,
            "packing_runtime_sec": run.diagnostics.policy_eval_sec,
        }
    )
    return payload


def _run_one_recording(
    *,
    speaker_id: str,
    recording_id: str,
    system_names: tuple[str, ...],
    paths: BuckeyePaths,
) -> dict[str, object]:
    loaded = load_recording(speaker_id, recording_id, paths=paths)
    try:
        if not loaded.buffers:
            raise RuntimeError(f"{recording_id} has no allowed buffers")
        spec_by_name = {spec.name: spec for spec in PHASE9_EXTERNAL_SPECS}
        need_external = any(name != "O0_4" for name in system_names)
        audio = None
        rate = loaded.sample_rate_hz
        if need_external:
            audio, rate = _load_audio_float32(loaded.audio_path)
        rows: list[dict[str, object]] = []
        unsafe: list[dict[str, object]] = []
        legality: list[dict[str, object]] = []
        signatures: dict[str, dict[str, object]] = {}
        rec_started = time.monotonic()
        for name in system_names:
            started = time.monotonic()
            if name == "O0_4":
                executed = _run_o0_4(loaded)
                result = executed.result
                diagnostics = executed.diagnostics
                score = evaluate_loaded(loaded, result)
                elapsed = time.monotonic() - started
                rows.append(
                    _recording_row(
                        speaker_id=speaker_id,
                        recording_id=recording_id,
                        geometry=name,
                        score=score,
                        diagnostics=diagnostics,
                        elapsed_sec=round(elapsed, 6),
                    )
                )
                unsafe.extend(_unsafe_cut_rows(geometry=name, loaded=loaded, result=result))
                signatures[name] = {
                    "policy_fingerprint": diagnostics.policy_fingerprint,
                    "geometry_fingerprint": diagnostics.geometry_fingerprint,
                    "candidate_cutpoint_sha256": diagnostics.candidate_cutpoint_sha256,
                    "selected_cutpoint_sha256": diagnostics.selected_cutpoint_sha256,
                    "selected_clip_sha256": diagnostics.selected_clip_sha256,
                    "evaluation": metrics_from_evaluation(score).to_dict(),
                }
                continue
            spec = spec_by_name[name]
            run = _run_external(loaded, spec, audio, rate)
            score = evaluate_loaded(loaded, run.result)
            elapsed = time.monotonic() - started
            rows.append(
                _recording_row(
                    speaker_id=speaker_id,
                    recording_id=recording_id,
                    geometry=name,
                    score=score,
                    diagnostics=run.diagnostics,
                    elapsed_sec=round(elapsed, 6),
                )
            )
            unsafe.extend(_unsafe_cut_rows(geometry=name, loaded=loaded, result=run.result))
            if spec.mode == "native":
                legality.append(
                    _legality_row(
                        speaker_id=speaker_id,
                        recording_id=recording_id,
                        spec=spec,
                        run=run,
                        elapsed_sec=elapsed,
                    )
                )
            signatures[name] = {
                "policy_fingerprint": run.diagnostics.policy_fingerprint,
                "geometry_fingerprint": run.diagnostics.geometry_fingerprint,
                "candidate_cutpoint_sha256": run.diagnostics.candidate_cutpoint_sha256,
                "selected_cutpoint_sha256": run.diagnostics.selected_cutpoint_sha256,
                "selected_clip_sha256": run.diagnostics.selected_clip_sha256,
                "evaluation": metrics_from_evaluation(score).to_dict(),
            }
        missing = [name for name in system_names if name not in signatures]
        if missing:
            raise RuntimeError(f"{recording_id} silently skipped baselines: {missing}")
        return {
            "speaker_id": speaker_id,
            "recording_id": recording_id,
            "rows": rows,
            "unsafe": unsafe,
            "legality": legality,
            "signatures": signatures,
            "elapsed_sec": time.monotonic() - rec_started,
        }
    finally:
        del loaded


def _parallel_job(job: dict[str, object]) -> dict[str, object]:
    return _run_one_recording(
        speaker_id=str(job["speaker_id"]),
        recording_id=str(job["recording_id"]),
        system_names=tuple(job["system_names"]),  # type: ignore[arg-type]
        paths=job["paths"],  # type: ignore[arg-type]
    )


def _order_spot_check(
    *,
    speaker_id: str,
    recording_id: str,
    paths: BuckeyePaths,
    system_names: tuple[str, ...],
) -> dict[str, object]:
    forward = system_names
    reverse = tuple(reversed(system_names))
    first = _run_one_recording(
        speaker_id=speaker_id,
        recording_id=recording_id,
        system_names=forward,
        paths=paths,
    )
    second = _run_one_recording(
        speaker_id=speaker_id,
        recording_id=recording_id,
        system_names=reverse,
        paths=paths,
    )
    compact: dict[str, object] = {
        "speaker_id": speaker_id,
        "recording_id": recording_id,
        "passed": True,
        "forward_order": list(forward),
        "reverse_order": list(reverse),
    }
    loaded = load_recording(speaker_id, recording_id, paths=paths)
    try:
        for name in system_names:
            left = first["signatures"][name]  # type: ignore[index]
            right = second["signatures"][name]  # type: ignore[index]
            for key in (
                "policy_fingerprint",
                "candidate_cutpoint_sha256",
                "selected_cutpoint_sha256",
                "selected_clip_sha256",
            ):
                if left[key] != right[key]:
                    raise RuntimeError(
                        f"{name} {key} changed across execution order on "
                        f"{speaker_id}/{recording_id}: {left[key]} vs {right[key]}"
                    )
            if left["evaluation"] != right["evaluation"]:
                raise RuntimeError(
                    f"{name} evaluator metrics changed across execution order on "
                    f"{speaker_id}/{recording_id}"
                )
            compact[name] = left
        if "O0_4" in system_names:
            a = _run_o0_4(loaded)
            b = _run_o0_4(loaded)
            if not same_contender_signatures(a.diagnostics, b.diagnostics):
                raise RuntimeError(
                    f"O0_4 diagnostics changed on repeat for {speaker_id}/{recording_id}"
                )
            assert_order_independent_scores(
                evaluate(loaded.reference, a.result),
                evaluate(loaded.reference, b.result),
                geometry_name="O0_4",
            )
    finally:
        del loaded
    return compact


def _blog_table(
    pooled: dict[str, object],
    *,
    native_comparable: dict[str, bool],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows: list[dict[str, object]] = []
    omitted: list[dict[str, object]] = []
    for name, mode in BLOG_TABLE_ROWS:
        metrics = pooled.get(name)
        if name.endswith("_native") and not native_comparable.get(name, True):
            omitted.append(
                {
                    "system": name,
                    "mode": mode,
                    "reason": "native output not directly benchmark-compatible",
                }
            )
            continue
        if metrics is None:
            omitted.append({"system": name, "mode": mode, "reason": "missing pooled metrics"})
            continue
        coverage = metrics.speech_coverage  # type: ignore[union-attr]
        gt50 = metrics.depth_gt_50ms_rate  # type: ignore[union-attr]
        gt100 = metrics.depth_gt_100ms_rate  # type: ignore[union-attr]
        rows.append(
            {
                "system": name,
                "mode": mode,
                "coverage": None if coverage is None else coverage * 100.0,
                "gt50_ms_pct": None if gt50 is None else gt50 * 100.0,
                "gt100_ms_pct": None if gt100 is None else gt100 * 100.0,
                "clips": metrics.clip_count,  # type: ignore[union-attr]
            }
        )
    return rows, omitted


def format_blog_table(rows: list[dict[str, object]]) -> str:
    header = (
        "| system                 | mode            | coverage |   >50ms |  >100ms | clips |"
    )
    rule = (
        "| ---------------------- | --------------- | -------: | ------: | ------: | ----: |"
    )
    lines = [header, rule]
    for row in rows:
        coverage = "n/a" if row["coverage"] is None else f"{row['coverage']:.4f}"
        gt50 = "n/a" if row["gt50_ms_pct"] is None else f"{row['gt50_ms_pct']:.4f}"
        gt100 = "n/a" if row["gt100_ms_pct"] is None else f"{row['gt100_ms_pct']:.4f}"
        lines.append(
            f"| {str(row['system']):<22} | {str(row['mode']):<15} | "
            f"{coverage:>8} | {gt50:>7} | {gt100:>7} | {row['clips']:>5} |"
        )
    return "\n".join(lines)


def run_phase9(
    *,
    output_dir: Path,
    subset: tuple[tuple[str, str], ...],
    selection_rule: str,
    workers: int = 2,
    system_names: tuple[str, ...] | None = None,
    spot_check_ids: Sequence[tuple[str, str]] | None = None,
    cohort_info: dict[str, object] | None = None,
    resume: bool = False,
) -> dict[str, object]:
    require_frozen_o0_4()
    vendor = require_openvpi_vendor()
    versions = _library_versions()
    names = _systems_in_order(system_names or PHASE9_SMOKE_SYSTEMS)
    if "O0_4" not in names:
        raise RuntimeError("Phase 9 must include frozen O0_4")
    for spec in PHASE9_EXTERNAL_SPECS:
        if spec.name not in names:
            raise RuntimeError(f"requested competitor silently omitted: {spec.name}")
    if workers < 1:
        raise RuntimeError(f"workers must be >= 1, got {workers}")
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError(
            f"cohort fingerprint changed: {cohort.fingerprint} != {PHASE5_COHORT_FINGERPRINT}"
        )
    missing = [item for item in subset if item not in set(cohort.recordings)]
    if missing:
        raise RuntimeError(f"subset recordings are not in the accepted cohort: {missing}")
    fingerprint = cohort_identity_fingerprint(subset)
    paths = BuckeyePaths.canonical()
    output_dir.mkdir(parents=True, exist_ok=True)
    recordings_path = output_dir / "recordings.csv"
    native_path = output_dir / "native_legality.csv"
    determinism_path = output_dir / "determinism_checks.json"
    manifest_path = output_dir / "manifest.json"
    summary_path = output_dir / "summary.json"
    if summary_path.exists():
        summary_path.unlink()
    if not resume:
        _unlink_outputs(output_dir)
        extra = output_dir / "native_legality.csv"
        if extra.exists():
            extra.unlink()

    spec_payloads = {spec.name: _spec_payload(spec) for spec in PHASE9_EXTERNAL_SPECS}
    manifest = {
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": 9,
        "adapter_version": ADAPTER_VERSION,
        "parameter_policy": PARAMETER_POLICY,
        "cohort_fingerprint": fingerprint,
        "full_cohort_fingerprint": cohort.fingerprint,
        "recording_count": len(subset),
        "selection_rule": selection_rule,
        "subset": [{"speaker_id": s, "recording_id": r} for s, r in subset],
        "systems": list(names),
        "internal_control": "O0_4",
        "o0_4_geometry_fingerprint": geometry_fingerprint(O0_4),
        "external_baselines": spec_payloads,
        "rvc": RVC_DECISION,
        "openvpi_vendor": vendor,
        "library_versions": versions,
        "public_cutpoint_population": PUBLIC_CUTPOINT_POPULATION,
        "workers": workers,
        "git_commit": git_head(output_dir.parent),
        "paths": {
            "normalized_root": str(paths.normalized_root),
            "cohort_root": str(paths.cohort_root),
        },
    }
    if cohort_info:
        manifest["cohort"] = cohort_info
    _write_json(manifest_path, manifest)

    started = time.monotonic()
    pending = list(subset)
    completed: dict[tuple[str, str], dict[str, object]] = {}
    if workers == 1:
        for speaker_id, recording_id in pending:
            print(f"{speaker_id}/{recording_id}", flush=True)
            completed[(speaker_id, recording_id)] = _run_one_recording(
                speaker_id=speaker_id,
                recording_id=recording_id,
                system_names=names,
                paths=paths,
            )
    else:
        print(f"parallel workers={workers} recordings={len(pending)}", flush=True)
        jobs = [
            {
                "speaker_id": speaker_id,
                "recording_id": recording_id,
                "system_names": names,
                "paths": paths,
            }
            for speaker_id, recording_id in pending
        ]
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=workers) as pool:
            received: set[tuple[str, str]] = set()
            for item in pool.imap_unordered(_parallel_job, jobs):
                key = (str(item["speaker_id"]), str(item["recording_id"]))
                if key in received:
                    raise RuntimeError(f"duplicate parallel result for {key}")
                received.add(key)
                completed[key] = item
                print(f"  committed {key[0]}/{key[1]} done={len(received)}/{len(pending)}", flush=True)
        if received != set(pending):
            raise RuntimeError("parallel worker results do not match pending recordings")

    recording_rows: list[dict[str, object]] = []
    unsafe_rows: list[dict[str, object]] = []
    legality_rows: list[dict[str, object]] = []
    for speaker_id, recording_id in subset:
        item = completed[(speaker_id, recording_id)]
        recording_rows.extend(item["rows"])  # type: ignore[arg-type]
        unsafe_rows.extend(item["unsafe"])  # type: ignore[arg-type]
        legality_rows.extend(item["legality"])  # type: ignore[arg-type]
    _write_csv_atomic(recordings_path, recording_rows)
    _write_csv_atomic(output_dir / "unsafe_cutpoints.csv", unsafe_rows)
    _write_csv_atomic(native_path, legality_rows)

    pooled: dict[str, object] = {}
    for name in names:
        rows = [row for row in recording_rows if str(row["geometry"]) == name]
        if len(rows) != len(subset):
            raise RuntimeError(f"{name} row count {len(rows)} != cohort {len(subset)}")
        pooled[name] = pool_metric_dicts(rows)

    native_comparable: dict[str, bool] = {}
    for spec in PHASE9_EXTERNAL_SPECS:
        if spec.mode != "native":
            continue
        rows = [row for row in legality_rows if str(row["system"]) == spec.name]
        legal = sum(int(row["legal_clip_count"]) for row in rows)
        native_comparable[spec.name] = legal > 0

    blog_rows, omitted_native = _blog_table(pooled, native_comparable=native_comparable)
    geometry_table = geometry_comparison_table(pooled, names)  # type: ignore[arg-type]

    if spot_check_ids is None:
        spot_ids = (subset[0], subset[min(1, len(subset) - 1)])
    else:
        spot_ids = tuple(spot_check_ids)
    print("determinism spot checks", flush=True)
    determinism = [
        _order_spot_check(
            speaker_id=speaker_id,
            recording_id=recording_id,
            paths=paths,
            system_names=names,
        )
        for speaker_id, recording_id in spot_ids
    ]
    _write_json(determinism_path, determinism)

    elapsed_sec = time.monotonic() - started
    elapsed_by_system = {
        name: sum(
            float(row["elapsed_sec"])
            for row in recording_rows
            if str(row["geometry"]) == name and row.get("elapsed_sec") not in (None, "")
        )
        for name in names
    }
    detector_runtime = {
        name: sum(
            float(row["vad_compute_sec"])
            for row in recording_rows
            if str(row["geometry"]) == name and row.get("vad_compute_sec") not in (None, "")
        )
        for name in names
        if name != "O0_4"
    }
    packing_runtime = {
        name: sum(
            float(row["policy_eval_sec"])
            for row in recording_rows
            if str(row["geometry"]) == name and row.get("policy_eval_sec") not in (None, "")
        )
        for name in names
        if name != "O0_4"
    }
    native_totals = {}
    for spec in PHASE9_EXTERNAL_SPECS:
        if spec.mode != "native":
            continue
        rows = [row for row in legality_rows if str(row["system"]) == spec.name]
        native_totals[spec.name] = {
            "raw_clip_count": sum(int(row["raw_clip_count"]) for row in rows),
            "raw_emitted_sec": sum(float(row["raw_emitted_sec"]) for row in rows),
            "clips_lt_3": sum(int(row["clips_lt_3"]) for row in rows),
            "clips_gt_15": sum(int(row["clips_gt_15"]) for row in rows),
            "legal_clip_count": sum(int(row["legal_clip_count"]) for row in rows),
        }
    summary = {
        "subset": [{"speaker_id": s, "recording_id": r} for s, r in subset],
        "selection_rule": selection_rule,
        "cohort_fingerprint": fingerprint,
        "full_cohort_fingerprint": cohort.fingerprint,
        "recording_count": len(subset),
        "systems": list(names),
        "elapsed_sec": elapsed_sec,
        "elapsed_by_system": elapsed_by_system,
        "detector_runtime_sec": detector_runtime,
        "packing_runtime_sec": packing_runtime,
        "workers": workers,
        "determinism_checks": determinism,
        "pooled": {name: metrics.to_dict() for name, metrics in pooled.items()},  # type: ignore[union-attr]
        "geometry_table": geometry_table,
        "blog_table": blog_rows,
        "omitted_native_rows": omitted_native,
        "native_legality_totals": native_totals,
        "library_versions": versions,
        "openvpi_vendor": vendor,
        "rvc": RVC_DECISION,
        "parameter_policy": PARAMETER_POLICY,
        "geometry_table_text": format_geometry_table(geometry_table, names),
        "blog_table_text": format_blog_table(blog_rows),
    }
    _write_json(summary_path, summary)
    return summary


def run_phase9_smoke(
    *,
    output_dir: Path,
    workers: int = 2,
    resume: bool = False,
) -> dict[str, object]:
    return run_phase9(
        output_dir=output_dir,
        subset=PHASE7_SMOKE_SUBSET,
        selection_rule=PHASE7_SMOKE_SELECTION_RULE,
        workers=workers,
        spot_check_ids=(PHASE7_SMOKE_SUBSET[0], ("s03", "s0301a")),
        resume=resume,
        cohort_info={
            "mode": "phase9_external_smoke",
            "fingerprint": derive_full_cohort().fingerprint,
            "smoke_recording_count": len(PHASE7_SMOKE_SUBSET),
        },
    )


def run_phase9_full(
    *,
    output_dir: Path,
    workers: int = 2,
    confirm_full_cohort: bool = False,
) -> dict[str, object]:
    if not confirm_full_cohort:
        raise RuntimeError("refusing to run the 120-recording Phase 9 cohort without --confirm-full-cohort")
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError("cohort fingerprint changed; abort")
    return run_phase9(
        output_dir=output_dir,
        subset=cohort.recordings,
        selection_rule=cohort.selection_rule,
        workers=workers,
        cohort_info={
            "mode": "phase9_external_full",
            "fingerprint": cohort.fingerprint,
            "recording_count": cohort.recording_count,
        },
    )
