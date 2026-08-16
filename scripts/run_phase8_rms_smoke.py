#!/usr/bin/env python3
"""Phase 8 smoke: O25_4 RMS evidence vs frozen A / O0_4 / O0_2 controls.

Does not launch the 120-recording RMS tournament.

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase8_rms_smoke.py --workers 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.buckeye_loader import (  # noqa: E402
    PHASE5_COHORT_FINGERPRINT,
    PHASE7_SMOKE_SELECTION_RULE,
    PHASE7_SMOKE_SUBSET,
    derive_full_cohort,
)
from adapter.buckeye_metrics import format_geometry_table  # noqa: E402
from adapter.buckeye_validate import run_validation  # noqa: E402
from adapter.config import PHASE8_SMOKE_CONTENDERS  # noqa: E402
from adapter.diagnostics import geometry_fingerprint, policy_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 O25_4 RMS smoke subset")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase8_rms_smoke"),
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    contenders = PHASE8_SMOKE_CONTENDERS
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError("cohort fingerprint drifted from Phase 5")
    missing = [item for item in PHASE7_SMOKE_SUBSET if item not in set(cohort.recordings)]
    if missing:
        raise RuntimeError(f"smoke subset recordings are not in the accepted cohort: {missing}")
    print("contenders:")
    for config in contenders:
        print(
            f"  {config.name}: geo={geometry_fingerprint(config)} pol={policy_fingerprint(config)}"
        )
    print(f"workers: {args.workers}")
    output_dir = Path(args.output_dir)
    summary = run_validation(
        output_dir=output_dir,
        subset=PHASE7_SMOKE_SUBSET,
        resume=not args.no_resume,
        selection_rule=PHASE7_SMOKE_SELECTION_RULE,
        spot_check_ids=(PHASE7_SMOKE_SUBSET[0], ("s03", "s0301a")),
        contenders=contenders,
        workers=args.workers,
        cohort_info={
            "mode": "phase8_rms_smoke",
            "fingerprint": cohort.fingerprint,
            "smoke_recording_count": len(PHASE7_SMOKE_SUBSET),
        },
    )
    if summary.get("geometry_table"):
        print()
        print(
            format_geometry_table(
                summary["geometry_table"],  # type: ignore[arg-type]
                [config.name for config in contenders],
            )
        )
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"runtime: {summary.get('runtime')}")
    print(f"O25_4_feature_reuse: {summary.get('O25_4_feature_reuse')}")
    print(f"O25_4_vad_computations_per_recording: {summary.get('O25_4_vad_computations_per_recording')}")
    print(f"wrote {output_dir}")
    print("phase 8 rms smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
