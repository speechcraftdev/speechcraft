#!/usr/bin/env python3
"""Phase 9 full accepted cohort. Do not run until smoke is reviewed.

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase9_external_full.py --confirm-full-cohort --workers 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.external_validate import run_phase9_full  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 9 external slicer full cohort")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase9_external_full"),
    )
    parser.add_argument(
        "--confirm-full-cohort",
        action="store_true",
        help="Required. Refuses the 120-recording run without this flag.",
    )
    args = parser.parse_args()
    summary = run_phase9_full(
        output_dir=Path(args.output_dir),
        workers=args.workers,
        confirm_full_cohort=args.confirm_full_cohort,
    )
    print(summary.get("blog_table_text") or "")
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
