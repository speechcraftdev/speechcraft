#!/usr/bin/env python3
"""Phase 5: full accepted Buckeye A/D validation.

Example (this environment):

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase5_full_ad_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.buckeye_loader import (  # noqa: E402
    default_spot_check_ids,
    derive_full_cohort,
)
from adapter.buckeye_metrics import format_comparison_table  # noqa: E402
from adapter.buckeye_validate import run_validation  # noqa: E402
from adapter.diagnostics import resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    cohort = derive_full_cohort()
    print(f"speakers: {cohort.speaker_count}")
    print(f"recordings: {cohort.recording_count}")
    print(f"allowed_buffer_duration_sec: {cohort.allowed_buffer_duration_sec:.6f}")
    print(f"cohort_fingerprint: {cohort.fingerprint}")
    print("canonical artifacts:")
    print(f"  normalized_root: {cohort.paths.normalized_root}")
    print(f"  cohort_root: {cohort.paths.cohort_root}")
    print(f"  cluster_mapping_csv: {cohort.paths.cluster_mapping_csv}")
    print(f"  speaker_cohort_summary_csv: {cohort.speaker_cohort_summary_csv}")
    print(f"  selection_rule: {cohort.selection_rule}")
    first, middle, last = (
        cohort.recordings[0],
        cohort.recordings[len(cohort.recordings) // 2],
        cohort.recordings[-1],
    )
    print(f"first: {first[0]}/{first[1]}")
    print(f"middle: {middle[0]}/{middle[1]}")
    print(f"last: {last[0]}/{last[1]}")
    spot_ids = default_spot_check_ids(cohort.recordings)
    print("spot checks:")
    for speaker_id, recording_id in spot_ids:
        print(f"  {speaker_id}/{recording_id}")
    output_dir = ROOT / "phase5_full_ad"
    summary = run_validation(
        output_dir=output_dir,
        subset=cohort.recordings,
        resume=True,
        selection_rule=cohort.selection_rule,
        spot_check_ids=spot_ids,
        cohort_info={
            "speaker_count": cohort.speaker_count,
            "recording_count": cohort.recording_count,
            "allowed_buffer_duration_sec": cohort.allowed_buffer_duration_sec,
            "fingerprint": cohort.fingerprint,
            "speaker_ids": list(cohort.speaker_ids),
            "canonical_paths": {
                "normalized_root": str(cohort.paths.normalized_root),
                "cohort_root": str(cohort.paths.cohort_root),
                "cluster_mapping_csv": str(cohort.paths.cluster_mapping_csv),
                "speaker_cohort_summary_csv": str(cohort.speaker_cohort_summary_csv),
            },
        },
    )
    if summary.get("recording_count") != cohort.recording_count:
        raise RuntimeError("summary recording count drifted from derived cohort")
    print()
    print(format_comparison_table(summary["comparison"]))  # type: ignore[arg-type]
    print()
    print("pooled A:", summary["pooled_A"])
    print("pooled D:", summary["pooled_D"])
    print("paired coverage:", summary["paired_coverage"])
    print("qualitative:", summary["qualitative"])
    print("historical comparison flags:", summary["historical_comparison"]["flags"])
    print("determinism checks:", summary["determinism_checks"])
    print("A/D independence:", summary["ad_independence_guard"])
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {output_dir}")
    if summary["historical_comparison"]["material_discrepancy"]:
        print("WARNING: material discrepancy vs historical proper A/D experiment; investigate")
        for flag in summary["historical_comparison"]["flags"]:
            print(f"  - {flag}")
    print("phase 5 full buckeye A/D validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
