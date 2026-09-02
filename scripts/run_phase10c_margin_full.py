#!/usr/bin/env python3
"""Phase 10C full: 120-recording signed-margin OOF vs O0_4 score-control.

Reuses the frozen 15-feature full table. Does not rerun Silero.

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase10c_margin_full.py --workers 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.buckeye_loader import (  # noqa: E402
    FULL_COHORT_SELECTION_RULE,
    PHASE5_COHORT_FINGERPRINT,
    derive_full_cohort,
)
from adapter.config import FEATURE_SCHEMA_ID, O0_4  # noqa: E402
from adapter.diagnostics import geometry_fingerprint, resolve_speaker_ts_eval_src  # noqa: E402
from adapter.margin_experiment import run_phase10c  # noqa: E402
from adapter.margin_labels import MARGIN_SCHEMA_ID  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 10C signed-margin full cohort")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase10c_margin_full"),
    )
    parser.add_argument(
        "--candidates-csv",
        default=str(ROOT / "phase10b_logistic_full" / "candidates.csv"),
        help="Frozen 15-feature table to relabel. Default: Phase 10B full extract.",
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
    candidates_csv = Path(args.candidates_csv)
    if not candidates_csv.is_file():
        raise RuntimeError(
            f"missing feature table {candidates_csv}; run Phase 10B full first "
            "or pass --candidates-csv"
        )
    summary = run_phase10c(
        output_dir=Path(args.output_dir),
        subset=cohort.recordings,
        workers=args.workers,
        resume=not args.no_resume,
        candidates_csv=candidates_csv,
        cohort_info={
            "mode": "phase10c_margin_full",
            "fingerprint": cohort.fingerprint,
            "recording_count": cohort.recording_count,
            "speaker_count": cohort.speaker_count,
            "selection_rule": FULL_COHORT_SELECTION_RULE,
            "candidates_csv": str(candidates_csv),
        },
    )
    print()
    print(summary.get("geometry_table_text") or "")
    print(f"n_candidates: {summary['n_candidates']}")
    print(f"label_counts: {summary['label_counts']}")
    print(f"label_reasons: {summary['label_reasons']}")
    print(f"ranking: {summary.get('ranking')}")
    print(f"schedule_diffs_vs_o0_4: {summary.get('schedule_diffs_vs_o0_4')}")
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {args.output_dir}")
    print("phase 10C signed-margin full passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
