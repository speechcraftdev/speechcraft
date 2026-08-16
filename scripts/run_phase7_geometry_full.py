#!/usr/bin/env python3
"""Phase 7 Stage 2: full accepted cohort for a caller-specified geometry subset.

Does not launch automatically from the smoke runner.

Example (not run during prep):

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase7_geometry_full.py \\
        --geometries A_O50_8,D_O0_8,O25_4,O0_4,O12.5_4,O0_2 --workers 2
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
    default_spot_check_ids,
    derive_full_cohort,
)
from adapter.buckeye_metrics import format_geometry_table  # noqa: E402
from adapter.buckeye_validate import run_validation  # noqa: E402
from adapter.config import resolve_geometries  # noqa: E402
from adapter.diagnostics import geometry_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7 full geometry subset")
    parser.add_argument(
        "--geometries",
        required=True,
        help="Comma-separated geometry names, e.g. A_O50_8,D_O0_8,O25_4,O0_4,O12.5_4,O0_2",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase7_geometry_full"),
        help="Output directory",
    )
    args = parser.parse_args()
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    geometries = resolve_geometries(args.geometries)
    fingerprints = [geometry_fingerprint(config) for config in geometries]
    if len(set(fingerprints)) != len(fingerprints):
        raise RuntimeError("requested geometry fingerprints are not unique")
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError("cohort fingerprint drifted from Phase 5")
    print("geometries:")
    for config in geometries:
        print(f"  {config.name}: {geometry_fingerprint(config)}")
    print(f"workers: {args.workers}")
    print(f"recordings: {cohort.recording_count}")
    print(f"cohort_fingerprint: {cohort.fingerprint}")
    output_dir = Path(args.output_dir)
    summary = run_validation(
        output_dir=output_dir,
        subset=cohort.recordings,
        resume=True,
        selection_rule=cohort.selection_rule,
        spot_check_ids=default_spot_check_ids(cohort.recordings)[:2],
        contenders=geometries,
        workers=args.workers,
        cohort_info={
            "mode": "full",
            "speaker_count": cohort.speaker_count,
            "recording_count": cohort.recording_count,
            "fingerprint": cohort.fingerprint,
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
    print("phase 7 geometry full subset passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
