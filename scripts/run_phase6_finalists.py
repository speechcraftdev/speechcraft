#!/usr/bin/env python3
"""Phase 6: A, D, min_quiet_run_64ms, quiet_run_score on the accepted Buckeye cohort.

Example:

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase6_finalists.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.buckeye_loader import (  # noqa: E402
    PHASE5_COHORT_FINGERPRINT,
    default_spot_check_ids,
    derive_full_cohort,
    load_recording,
)
from adapter.buckeye_metrics import format_four_way_table  # noqa: E402
from adapter.buckeye_validate import run_geometry_isolated, run_validation  # noqa: E402
from adapter.config import (  # noqa: E402
    CURRENT_A,
    MIN_QUIET_RUN_64MS,
    PHASE6_CONTENDERS,
    PROPER_D,
    QUIET_RUN_SCORE,
)
from adapter.diagnostics import geometry_fingerprint, policy_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402


def _assert_identities() -> None:
    a_geo = geometry_fingerprint(CURRENT_A)
    d_geo = geometry_fingerprint(PROPER_D)
    if a_geo == d_geo:
        raise RuntimeError("A and D geometry fingerprints collapsed")
    if geometry_fingerprint(MIN_QUIET_RUN_64MS) != a_geo:
        raise RuntimeError("min_quiet_run_64ms is not using A geometry")
    if geometry_fingerprint(QUIET_RUN_SCORE) != a_geo:
        raise RuntimeError("quiet_run_score is not using A geometry")
    a_pol = policy_fingerprint(CURRENT_A)
    if policy_fingerprint(MIN_QUIET_RUN_64MS) == a_pol:
        raise RuntimeError("min_quiet_run_64ms policy fingerprint matches A")
    if policy_fingerprint(QUIET_RUN_SCORE) == a_pol:
        raise RuntimeError("quiet_run_score policy fingerprint matches A")
    if policy_fingerprint(MIN_QUIET_RUN_64MS) == policy_fingerprint(QUIET_RUN_SCORE):
        raise RuntimeError("the two A-derived policy fingerprints match each other")


def _schedule_sig(run) -> tuple[str, str, str]:
    diag = run.diagnostics
    return (
        diag.candidate_cutpoint_sha256,
        diag.selected_cutpoint_sha256,
        diag.selected_clip_sha256,
    )


def _assert_policies_not_silently_a() -> None:
    """Cheap wiring check on known recordings before the 120-recording run."""
    min_ok = False
    quiet_ok = False
    for speaker_id, recording_id in (("s01", "s0101b"), ("s03", "s0301a")):
        print(f"policy wiring preflight: {speaker_id}/{recording_id}", flush=True)
        loaded = load_recording(speaker_id, recording_id)
        try:
            a_sig = _schedule_sig(run_geometry_isolated(loaded, CURRENT_A))
            min_sig = _schedule_sig(run_geometry_isolated(loaded, MIN_QUIET_RUN_64MS))
            quiet_sig = _schedule_sig(run_geometry_isolated(loaded, QUIET_RUN_SCORE))
        finally:
            del loaded
        min_differs = min_sig != a_sig
        quiet_differs = quiet_sig != a_sig
        print(
            f"  min_quiet_run_64ms differs from A: {min_differs}",
            flush=True,
        )
        print(
            f"  quiet_run_score differs from A: {quiet_differs}",
            flush=True,
        )
        min_ok = min_ok or min_differs
        quiet_ok = quiet_ok or quiet_differs
        if min_ok and quiet_ok:
            return
    silent = []
    if not min_ok:
        silent.append("min_quiet_run_64ms")
    if not quiet_ok:
        silent.append("quiet_run_score")
    raise RuntimeError(
        "A-derived policies were identical to A on s0101b and s0301a: "
        f"{silent}. Policy config is probably not wired."
    )


def main() -> int:
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    _assert_identities()
    print("geometry fingerprints:")
    for config in PHASE6_CONTENDERS:
        print(f"  {config.name}: {geometry_fingerprint(config)}")
    print("policy fingerprints:")
    for config in PHASE6_CONTENDERS:
        print(f"  {config.name}: {policy_fingerprint(config)}")
    _assert_policies_not_silently_a()
    cohort = derive_full_cohort()
    print(f"speakers: {cohort.speaker_count}")
    print(f"recordings: {cohort.recording_count}")
    print(f"cohort_fingerprint: {cohort.fingerprint}")
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError(
            "cohort fingerprint drifted from the corrected Phase-5 run: "
            f"{cohort.fingerprint} != {PHASE5_COHORT_FINGERPRINT}"
        )
    spot_ids = default_spot_check_ids(cohort.recordings)
    print("spot checks:")
    for speaker_id, recording_id in spot_ids:
        print(f"  {speaker_id}/{recording_id}")
    output_dir = ROOT / "phase6_finalists"
    summary = run_validation(
        output_dir=output_dir,
        subset=cohort.recordings,
        resume=True,
        selection_rule=cohort.selection_rule,
        spot_check_ids=spot_ids,
        contenders=PHASE6_CONTENDERS,
        cohort_info={
            "speaker_count": cohort.speaker_count,
            "recording_count": cohort.recording_count,
            "allowed_buffer_duration_sec": cohort.allowed_buffer_duration_sec,
            "fingerprint": cohort.fingerprint,
            "canonical_paths": {
                "normalized_root": str(cohort.paths.normalized_root),
                "cohort_root": str(cohort.paths.cohort_root),
                "cluster_mapping_csv": str(cohort.paths.cluster_mapping_csv),
                "speaker_cohort_summary_csv": str(cohort.speaker_cohort_summary_csv),
            },
        },
    )
    print()
    if summary.get("four_way"):
        print(format_four_way_table(summary["four_way"]))  # type: ignore[arg-type]
    print()
    print("delta vs A:", summary.get("delta_vs_A"))
    print("paired vs A:", summary.get("paired_vs_A"))
    print("pareto:", summary.get("pareto"))
    print("schedule differed from A:", summary.get("schedule_differed_from_A_recordings"))
    print("determinism:", summary.get("ad_independence_guard"), "spot checks passed")
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {output_dir}")
    print("phase 6 four-contender validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
