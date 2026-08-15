#!/usr/bin/env python3
"""Phase 7 Stage 1: geometry smoke subset. Do not auto-launch the full sweep.

Example (not run during prep):

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase7_geometry_smoke.py --workers 1
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
from adapter.config import PHASE7_GEOMETRIES, resolve_geometries  # noqa: E402
from adapter.diagnostics import geometry_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7 geometry smoke subset")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--geometries",
        default=",".join(config.name for config in PHASE7_GEOMETRIES),
        help="Comma-separated geometry names",
    )
    args = parser.parse_args()
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    geometries = resolve_geometries(args.geometries)
    fingerprints = [geometry_fingerprint(config) for config in geometries]
    if len(set(fingerprints)) != len(fingerprints):
        raise RuntimeError("Phase 7 geometry fingerprints are not unique")
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError("cohort fingerprint drifted from Phase 5")
    missing = [item for item in PHASE7_SMOKE_SUBSET if item not in set(cohort.recordings)]
    if missing:
        raise RuntimeError(f"smoke subset recordings are not in the accepted cohort: {missing}")
    print("geometries:")
    for config in geometries:
        print(f"  {config.name}: {geometry_fingerprint(config)}")
    print(f"workers: {args.workers}")
    print("smoke subset:")
    for speaker_id, recording_id in PHASE7_SMOKE_SUBSET:
        print(f"  {speaker_id}/{recording_id}")
    output_dir = ROOT / "phase7_geometry_smoke"
    summary = run_validation(
        output_dir=output_dir,
        subset=PHASE7_SMOKE_SUBSET,
        resume=True,
        selection_rule=PHASE7_SMOKE_SELECTION_RULE,
        spot_check_ids=(PHASE7_SMOKE_SUBSET[0], ("s03", "s0301a")),
        contenders=geometries,
        workers=args.workers,
        cohort_info={
            "mode": "smoke",
            "fingerprint": cohort.fingerprint,
            "smoke_recording_count": len(PHASE7_SMOKE_SUBSET),
        },
    )
    if summary.get("geometry_table"):
        print()
        print(
            format_geometry_table(
                summary["geometry_table"],  # type: ignore[arg-type]
                [config.name for config in geometries],
            )
        )
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {output_dir}")
    print("phase 7 geometry smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
