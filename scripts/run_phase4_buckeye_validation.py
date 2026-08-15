#!/usr/bin/env python3
"""Phase 4: real Buckeye A/D subset validation.

Example (this environment):

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_phase4_buckeye_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.buckeye_loader import VALIDATION_SUBSET, validation_subset  # noqa: E402
from adapter.buckeye_metrics import format_comparison_table  # noqa: E402
from adapter.buckeye_validate import run_validation  # noqa: E402
from adapter.diagnostics import resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    subset = validation_subset()
    print("subset:")
    for speaker_id, recording_id in subset:
        print(f"  {speaker_id}/{recording_id}")
    if subset != VALIDATION_SUBSET:
        raise RuntimeError("subset drifted from the frozen VALIDATION_SUBSET constant")
    output_dir = ROOT / "phase4_validation"
    summary = run_validation(output_dir=output_dir)
    print()
    print(format_comparison_table(summary["comparison"]))  # type: ignore[arg-type]
    print()
    print("qualitative:", summary["qualitative"])
    print(f"order independence: {summary['order_independence_recording']}")
    print(f"A/D signatures differed: {summary['ad_observation_signatures_differed']}")
    print(f"wrote {output_dir}")
    print("phase 4 buckeye validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
