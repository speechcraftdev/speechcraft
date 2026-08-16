# Phase 1 Referee

Small independent evaluation layer for slicer outputs. No real slicer runs here.

## Contract

```text
RecordingReference + SlicerResult(cutpoints, clips) -> EvaluationResult
```

`RecordingReference` carries allowed buffers plus evaluator-only annotations (trusted phones, uncertainty masks). Those annotations are never inputs to a slicer.

`SlicerResult.cutpoints` is the **selected-schedule** population: unique detector cutpoints used as endpoints of the final packed clips. It is not the detector candidate pool. The evaluator scores whatever population the adapter hands it; handing it candidates makes unique-cut safety meaningless.

## Populations

- **Primary (unique cut safety):** detector-selected internal cutpoints, deduplicated by `(recording_id, buffer_id, time_sec)`.
- **Secondary (final-edge safety):** every emitted clip start and end, counted as occurrences (a shared boundary used as one clip’s end and another’s start counts twice).

Outside a trusted phone means **not demonstrated unsafe**, not “safe.”

## Phone / coverage rules

- Inside phone `[a, b]` only when `a < t < b` (exact endpoints are outside).
- Depth thresholds use strict `>` (20 / 50 / 100 ms).
- Threshold classification uses integer microseconds (nearest µs) so float subtraction artifacts cannot turn an intended exact 20/50/100 ms depth into a false `>` hit.
- Overlapping trusted phones within one buffer are rejected (canonical Buckeye truth should not contain them).
- Eligible speech = trusted phones minus the **union** of uncertainty masks.
- Retained speech = eligible speech covered by the **union** of emitted clips per `(recording_id, buffer_id)`.
- If eligible duration is zero, `speech_coverage` is `None`.

## Phase 1 scope

Pure types, validation, interval arithmetic, and synthetic unit tests only.

## Phase 2 (execution adapters)

- Adds A/D execution adapters that return Phase-1 `SlicerResult` only.
- A and D share one adapter path and differ only by explicit immutable geometry config.
- Annotations remain evaluator-only; the adapter request cannot carry phones/uncertainty/reference labels.
- Phase 2 intentionally performs no shared acoustic caching between runs (including A vs D).

## Phase 3 (A/D smoke + geometry guard)

- Compact private execution diagnostics (fingerprints + observation hashes) sit beside `SlicerResult`; the evaluator stays geometry-blind.
- Real canonical A and D must produce different geometry fingerprints and must not share workdirs/cache artifacts or collapse onto identical VAD observation signatures.
- Still no shared acoustic cache.

## Phase 4 (small real Buckeye A/D validation)

- Fixed subset: first accepted recording with a target NeMo mapping from speakers s01–s04.
- A and D run independently on frozen raw NeMo scopes; the evaluator alone sees Buckeye phones/uncertainty.
- Compact outputs: `phase4_validation/manifest.json`, `summary.json`, `recordings.csv`.

## Phase 5 (full accepted Buckeye A/D)

- Same frozen ruler as Phase 4, scaled to every accepted recording in the canonical cohort artifacts.
- Sequential A then D per recording, no shared acoustic cache, independence guard on every recording.
- Pooled percentages from pooled counts; paired D−A coverage is a separate statistic.
- Compact outputs: `phase5_full_ad/manifest.json`, `summary.json`, `recordings.csv`, `unsafe_cutpoints.csv`.
- Historical paired coverage had 34 ties on a 153-row table that included review-required / zero-coverage extras. The rebuilt 120-recording accepted cohort is expected to have far fewer coverage ties; do not treat 34 vs 1 as a geometry discrepancy.

## Phase 5.1 (selected-cutpoint population)

- Adapter bug: Phase 5 first put the detector candidate pool into `SlicerResult.cutpoints` (~4× historical unique selected cuts). Clip/emitted counts were already correct.
- Fix: public cutpoints are the unique cutpoints used by the selected schedule. Evaluator unchanged.

## Phase 6 (two A-derived finalists)

- Same accepted 120-recording cohort, evaluator, selected-schedule population, and packing as Phase 5.
- Adds only `min_quiet_run_64ms` and `quiet_run_score` on A geometry (`window=512`, `hop=256`, `offsets=(0,128)`). D stays D.
- One execution path; policy knobs are `GeometryConfig.min_quiet_run_ms` / `scoring`. No shared acoustic cache.
- Compact outputs: `phase6_finalists/manifest.json`, `summary.json`, `recordings.csv`, `unsafe_cutpoints.csv`, `determinism_checks.json`.
- Character-dataset replay of `failure_27_bad_end` / `failure_49_bad_start` is still out of scope here.
- Full 120-recording result: both A-derived policies reduced shallow inside-phone overlap vs A, lost coverage vs A, and **worsened >50 ms and >100 ms rates**. Neither is a deep-error win over A; D remains the safer deep-error baseline.

## Phase 7 prep (geometry sweep harness)

- Seven smoke geometries: `A_O50_8`, `D_O0_8`, `O75_8`, `O25_8`, `O50_4`, `O25_4`, `O0_4`. Detector/RMS/packer/evaluator unchanged.
- Post-smoke finalists (not another giant sweep): `A_O50_8`, `D_O0_8`, `O25_4`, `O0_4`, plus `O12.5_4` (`window=512`, `hop=448`, offsets every 64 in `[0,448)`) and `O0_2` (`window=512`, `hop=512`, offsets every 32 in `[0,512)`).
- `A_O50_8` / `D_O0_8` keep the trusted A/D fingerprints; names differ from `current_A` / `proper_D`.
- Optional recording-level `--workers N` (default 1, spawn for real Silero). No VAD/RMS feature cache. Parallel mode commits each finished recording to CSV immediately.
- Distinct requested geometries must not collapse onto identical VAD observation signatures; diagnostics fingerprints must match the requested config.
- `RESET-8` (per-window Silero reset) is omitted: the canonical path resets once per offset stream, not per window.

## Phase 8 (O25_4 RMS evidence)

- Geometry is frozen: `A_O50_8`, `O25_4`, `O0_4`, `O0_2`. New work is RMS/pause evidence on `O25_4` only.
- All O25_4 RMS variants share one immutable in-memory VAD/RMS bundle per recording. Cross-geometry bundle reuse is rejected.
- Public `SlicerResult.cutpoints` remains selected-schedule. Evaluator unchanged.
- Round-1 seven RMS scorers (prominence / multiscale / valley / bilateral / min-placement) were dropped after smoke: none improved `>50 ms` vs `O25_4_CURRENT`. The reusable O25_4 feature-sharing harness stays.
- Round 2 is a veto against suspicious VAD valleys, interrogating the **raw waveform at 10 ms / 8 ms hops**, not 32 ms VAD-frame RMS:
  - `O25_4_WEAK_VALLEY_VETO` — reject only extremely shallow local RMS depressions (`veto_depth_db=1.5`)
  - `O25_4_SHORT_SHALLOW_PENALTY` — short valleys are not automatically bad; short **and** shallow are
  - `O25_4_WAVEFORM_SILENCE_RATIO` — fraction of ±64 ms below a recording-relative 10th-percentile fine RMS threshold
- Policy fingerprints include only knobs that affect that kind. Dead `prominence_db` is gone.
- Round 2 local RMS evidence is clipped to the candidate's allowed buffer. Recording percentiles use the union of allowed-buffer fine hops, not the whole WAV. One `FineRmsGrid` is computed per O25_4 recording and reused across the three policies.
- Smoke: `scripts/run_phase8_rms_smoke.py` (A / O25_4 family / O0_4 / O0_2). Do not run the 120-recording RMS tournament until smoke is reviewed.

## Phase 9 (external slicer baselines vs frozen O0_4)

- Internal control is frozen `O0_4` only. Do not include A, O25_4, O0_2, or RMS variants in the main table.
- External families: vendored OpenVPI `slicer2.py`, `librosa.effects.split`, pydub `detect_silence` / `detect_nonsilent`, FFmpeg `silencedetect`. RVC/slicer2 is attribution-only (same algorithm).
- Two modes where meaningful: native tool chunks vs external candidates into the frozen common 3–15 s packer. Every tool sees only allowed-buffer audio.
- Parameters are documented/upstream defaults chosen before Buckeye scores. Evaluator and O0_4 are unchanged.
- Smoke: `scripts/run_phase9_external_smoke.py` (10 recordings). Do not run the 120-recording external benchmark until smoke is reviewed.
