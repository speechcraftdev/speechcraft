#!/usr/bin/env python3
"""Phase 9 smoke: frozen O0_4 vs OpenVPI / librosa / pydub / FFmpeg.

Does not launch the 120-recording external benchmark.

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase9_external_smoke.py --workers 2
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.external_validate import run_phase9_smoke  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 9 external slicer smoke")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--output-dir",
        default=str(ROOT / "phase9_external_smoke"),
    )
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    summary = run_phase9_smoke(
        output_dir=Path(args.output_dir),
        workers=args.workers,
        resume=not args.no_resume,
    )
    print()
    print(summary.get("blog_table_text") or "")
    print()
    print(summary.get("geometry_table_text") or "")
    print(f"elapsed_sec: {summary['elapsed_sec']}")
    print(f"native_legality_totals: {summary.get('native_legality_totals')}")
    print(f"omitted_native_rows: {summary.get('omitted_native_rows')}")
    print(f"library_versions: {summary.get('library_versions')}")
    print(f"rvc: {summary.get('rvc')}")
    print(f"wrote {args.output_dir}")
    print("phase 9 external smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
