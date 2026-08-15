#!/usr/bin/env python3
"""Real canonical A/D smoke: Silero VAD + vad_percentile_rms on a tiny local WAV.

Fast unit tests do not run this. Needs numpy/torch/soundfile/silero-vad plus
speaker_ts_eval (see SPEAKER_TS_EVAL_SRC).

Example (this environment):

    /home/aaravthegreat/Projects/speechcraft/workers/dataset/.venv/bin/python \\
        scripts/run_canonical_ad_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapter.canonical_smoke import (  # noqa: E402
    assert_a_d_not_collapsed,
    assert_order_independence,
    assert_valid_public_result,
    compact_summary,
    prepare_fixture_wav,
    run_ad_order_smoke,
)
from adapter.diagnostics import resolve_speaker_ts_eval_src  # noqa: E402


def main() -> int:
    src = resolve_speaker_ts_eval_src()
    print(f"speaker_ts_eval src: {src}")
    with tempfile.TemporaryDirectory(prefix="buckeye_canonical_smoke_") as tmp:
        audio_path = prepare_fixture_wav(Path(tmp))
        print(f"fixture wav: {audio_path}")
        runs = run_ad_order_smoke(audio_path)
        for label, run in runs.items():
            print(f"validating public SlicerResult for {label}")
            assert_valid_public_result(audio_path, run.result)
        assert_a_d_not_collapsed(
            runs["A_in_AD"].diagnostics,
            runs["D_in_AD"].diagnostics,
        )
        assert_a_d_not_collapsed(
            runs["A_in_DA"].diagnostics,
            runs["D_in_DA"].diagnostics,
        )
        assert_order_independence(runs)
        summary = compact_summary(runs)
        summary["passed"] = True
        print(json.dumps(summary, indent=2, sort_keys=True))
        print("canonical A/D smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
