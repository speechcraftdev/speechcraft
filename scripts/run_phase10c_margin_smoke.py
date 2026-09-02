#!/usr/bin/env python3
"""Phase 10C smoke: O0_4 score-control + signed-margin OOF regressor.

Reuses the frozen 15-feature smoke table. Does not launch the 120-recording run.

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase10c_margin_smoke.py
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
from adapter.config import FEATURE_SCHEMA_ID, O0_4  # noqa: E402
from adapter.diagnostics import geometry_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402
from adapter.margin_experiment import run_phase10c  # noqa: E402
from adapter.margin_labels import MARGIN_SCHEMA_ID  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 10C signed-margin smoke subset")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase10c_margin_smoke"),
    )
    parser.add_argument(
        "--candidates-csv",
        default=str(ROOT / "phase10b_logistic_smoke" / "candidates.csv"),
        help="Frozen 15-feature table to relabel. Default: Phase 10B smoke extract.",
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    print(f"schema_id: {FEATURE_SCHEMA_ID}")
    print(f"margin_schema_id: {MARGIN_SCHEMA_ID}")
    print(f"O0_4 geo: {geometry_fingerprint(O0_4)}")
    cohort = derive_full_cohort()
    if cohort.fingerprint != PHASE5_COHORT_FINGERPRINT:
        raise RuntimeError("cohort fingerprint drifted from Phase 5")
    missing = [item for item in PHASE7_SMOKE_SUBSET if item not in set(cohort.recordings)]
    if missing:
        raise RuntimeError(f"smoke subset recordings are not in the accepted cohort: {missing}")
    candidates_csv = Path(args.candidates_csv)
    if not candidates_csv.is_file():
        raise RuntimeError(
            f"missing feature table {candidates_csv}; run Phase 10B smoke first "
            "or pass --candidates-csv"
        )
    summary = run_phase10c(
        output_dir=Path(args.output_dir),
        subset=PHASE7_SMOKE_SUBSET,
        workers=args.workers,
        resume=not args.no_resume,
        candidates_csv=candidates_csv,
        cohort_info={
            "mode": "phase10c_margin_smoke",
            "fingerprint": cohort.fingerprint,
            "smoke_recording_count": len(PHASE7_SMOKE_SUBSET),
            "selection_rule": PHASE7_SMOKE_SELECTION_RULE,
            "candidates_csv": str(candidates_csv),
        },
    )
    print()
    print(summary.get("geometry_table_text") or "")
    print(f"n_candidates: {summary['n_candidates']}")
    print(f"label_counts: {summary['label_counts']}")
    print(f"label_reasons: {summary['label_reasons']}")
    print(f"ranking: {summary['ranking']}")
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {args.output_dir}")
    print("phase 10C signed-margin smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
