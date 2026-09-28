# Handoff: Buckeye Silero VAD Geometry Experiment

## Current Task

User asked to run a Buckeye experiment comparing VAD extraction geometries for `vad_percentile_rms`, isolating only Silero geometry:

- `A_current_overlap_8ms`: `window=512`, `hop=256`, `offsets=[0,128]`
- `D_four_sequential_8ms`: `window=512`, `hop=512`, `offsets=[0,128,256,384]`
- `C_two_sequential_16ms`: `window=512`, `hop=512`, `offsets=[0,256]`
- `B_standard_sequential_32ms`: `window=512`, `hop=512`, `offsets=[0]`

Rules: do not tune thresholds, keep NeMo accepted cohort fixed, use existing repaired evaluator/packer, compare A vs D primarily, generate paired summaries and review bundle.

## Important Current State

The full geometry run is currently still running in the background.

Check it:

```bash
pgrep -af 'run_buckeye_vad_geometry_experiment|pytest|silero|onnx'
```

At last check:

```text
51954 uv run --with soundfile --with silero-vad --with onnxruntime --with matplotlib python scripts/run_buckeye_vad_geometry_experiment.py
52001 .../bin/python scripts/run_buckeye_vad_geometry_experiment.py
```

Progress at last check:

```text
19 completed detector-speaker runs out of 92
experiment root size: 3.4G
free disk: 24G
```

Progress command:

```bash
find /home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment/matrices \
  -path '*tables/invariant_report.csv' 2>/dev/null | wc -l
```

Do not trust a partial bundle unless the script exits successfully and writes provenance/bundle.

## Files Changed

### `/home/aaravthegreat/Projects/speaker_ts_eval/src/speaker_ts_eval/buckeye_safecut_benchmark.py`

Added env-controlled VAD backend/geometry:

```python
_vad_frame_geometry_from_env()
```

Environment variables:

```text
SPEAKER_TS_EVAL_VAD_BACKEND
SPEAKER_TS_EVAL_VAD_WINDOW_SAMPLES
SPEAKER_TS_EVAL_VAD_HOP_SAMPLES
SPEAKER_TS_EVAL_VAD_OFFSETS
```

`_compute_recording_vad_frames()` now:

- supports `torch_current` and `silero_official_onnx`;
- uses `load_silero_vad(onnx=...)`;
- resets Silero state once per offset stream;
- uses env `window/hop/offsets`;
- writes geometry-specific VAD cache files:

```text
{recording_id}_{backend}_w{window}_h{hop}_o{offsets}.json
```

Frame rows now include:

```text
vad_backend
vad_window_samples
vad_hop_samples
```

### `/home/aaravthegreat/Projects/speaker_ts_eval/src/speaker_ts_eval/repaired_buckeye_benchmark.py`

Imports `_vad_frame_geometry_from_env`.

Acoustic repaired dry runs now write active VAD geometry into `provenance.json`:

```text
vad_backend
vad_window_samples
vad_hop_samples
vad_offsets
```

No detector thresholds, packer behavior, NeMo scope, or evaluator logic were intentionally changed.

### `/home/aaravthegreat/Projects/speaker_ts_eval/tests/test_buckeye_safecut_benchmark.py`

Added unit tests:

- default geometry is current overlap-8ms: `torch_current`, `512/256`, offsets `(0,128)`;
- four-sequential override parses correctly: `silero_official_onnx`, `512/512`, offsets `(0,128,256,384)`;
- invalid offset `>= hop` is rejected.

Full suite result before launching the long run:

```text
159 passed, 1 warning
```

Command used:

```bash
cd /home/aaravthegreat/Projects/speaker_ts_eval
uv run --with pytest python -m pytest -q
```

### `/home/aaravthegreat/Projects/speechcraft/scripts/run_buckeye_vad_geometry_experiment.py`

New driver script.

It:

- runs tests first and saves `test_log.txt`;
- loads accepted speakers from:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort/speaker_cohort_summary.csv
```

- includes all speakers with `accepted_recording_count > 0`, which is 23 speakers including primary and supplementary;
- runs `run_repaired_detector_matrix()` for each geometry with only `vad_percentile_rms`;
- uses the accepted cohort eval roots:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort/eval_runs/<speaker>
```

- uses normalized references:

```text
/home/aaravthegreat/Datasets/buckeye/normalized_reviewed_2026-07-18/corpus/<speaker>
```

- uses accepted mapping CSV:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-18_buckeye_acoustic_matrix_v1/cohort/benchmark_cluster_mapping.csv
```

- writes output under:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment
```

- planned final bundle path:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment_review_bundle_no_audio.zip
```

## Commands Already Run

First attempt failed immediately because plain `python` lacked `soundfile`:

```bash
python scripts/run_buckeye_vad_geometry_experiment.py
```

Failure:

```text
ModuleNotFoundError: No module named 'soundfile'
```

Second attempt failed after completing first geometry because the `uv --with` environment lacked `matplotlib` for matrix figures:

```bash
uv run --with soundfile --with silero-vad --with onnxruntime python scripts/run_buckeye_vad_geometry_experiment.py
```

Failure:

```text
ModuleNotFoundError: No module named 'matplotlib'
```

Current running command:

```bash
uv run --with soundfile --with silero-vad --with onnxruntime --with matplotlib \
  python scripts/run_buckeye_vad_geometry_experiment.py
```

The script deletes/recreates its output root at startup, so the current run is fresh relative to failed attempts.

## Expected Outputs If Run Completes

Experiment root:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment
```

Aggregate tables:

```text
tables/scorecards.csv
tables/coverage.csv
tables/unique.csv
tables/final_edges.csv
tables/runtime.csv
tables/invariants.csv
tables/run_index.csv
tables/paired_speaker_deltas_vs_A.csv
tables/paired_recording_deltas_vs_A.csv
tables/paired_delta_summary.csv
tables/paired_A_vs_D_boundary_review_manifest.csv
```

Bundle:

```text
/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment_review_bundle_no_audio.zip
```

The bundle should include:

- aggregate tables;
- per-geometry matrix summary tables;
- cohort CSVs;
- test log;
- code snapshot for driver and touched evaluator/VAD code;
- no audio.

## Caveats / Things To Check

1. The run is currently active. If handing off and wanting a clean restart, kill it first:

```bash
kill 51954 52001
```

Then remove the experiment root before restarting:

```bash
rm -rf /home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment
```

2. The script runs geometries sequentially. Total expected runs:

```text
4 geometries × 23 speakers = 92 detector-speaker runs
```

3. The matrix runner still generates figures for each geometry, so `matplotlib` is required.

4. The script currently uses all speakers with accepted recordings, including supplementary `s05` and `s21`. If Linus wants primary-only, modify `load_speakers()` to filter `analysis_tier == "primary"`.

5. It packages no audio. The requested Buckeye review set is currently a timestamp manifest, not extracted WAV snippets. That matches “review bundle” constraints unless the user explicitly wants audio.

6. `speechcraft` repo has many unrelated dirty/untracked files. Do not revert them. The relevant new file here is:

```text
scripts/run_buckeye_vad_geometry_experiment.py
```

7. `/home/aaravthegreat/Projects/speaker_ts_eval` does not appear to report git status in this environment; treat changes there as local code edits that must be included in the bundle.

## Immediate Next Steps For Next Agent

1. Poll the running process:

```bash
pgrep -af 'run_buckeye_vad_geometry_experiment|pytest|silero|onnx'
```

2. Check progress:

```bash
find /home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment/matrices \
  -path '*tables/invariant_report.csv' 2>/dev/null | wc -l
```

3. If the process exits successfully, inspect:

```bash
cat /home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment/provenance.json
```

4. Verify invariant failures:

```bash
python - <<'PY'
import csv
from pathlib import Path
p = Path('/home/aaravthegreat/Datasets/buckeye/eval_runs/2026-07-21_buckeye_vad_geometry_experiment/tables/invariants.csv')
rows = list(csv.DictReader(p.open()))
print(sum(int(r['failed_count']) for r in rows), 'failed invariants across', len(rows), 'rows')
PY
```

5. Report:

```text
tests passed
completed detector-speaker runs
invariant failures
A vs D coverage/safety deltas
bundle path
free disk remaining
```

