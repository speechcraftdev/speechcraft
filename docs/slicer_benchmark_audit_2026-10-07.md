# Slicer benchmark audit — 2026-10-07

## Result

Grade: **C for the validation used to justify replacing ASR+MFA**.
Verdict: **FIX THESE ISSUES**. This is not evidence that ASR+MFA wins; the paired comparison is still missing.

The saved July 18 acoustic matrix has eight detectors, none ASR+MFA. Verified from `matrix/provenance.json` and `matrix/tables/run_index.csv` inside `../buckeye-slicer-lab/review_bundles/2026-07-18_buckeye_acoustic_matrix_review_bundle_no_audio.zip`.
`legacy_vad_rms` is not the old ASR+MFA slicer. The later Phase 7 geometry and Phase 9 external-baseline results also omit ASR+MFA.
There is an implementation of the comparison in `../speaker_ts_eval/src/speaker_ts_eval/repaired_buckeye_benchmark.py::run_repaired_production_dry_run`, but code existing is not evidence it ran. No completed matched ASR+MFA result was found in the inspected artifacts.

## Confirmed metric limitations

1. `buckeye-slicer-lab/referee/evaluate.py::_leak_depth_inside_phone` tests strict phone interiors. A cut at a phone boundary inside a word has no penalty. The referee types do not carry words or phrase boundaries. Synthetic reproduction: two adjacent 3-second clips split a word between its two phones, retain 100% of reference speech, and incur zero phone-cut penalties.
2. `_safety_counts` divides by all selected cuts, including cuts with no trusted phone evidence. Synthetic reproduction: add a legal clip outside the annotated span; the same one known bad cut changes from 1/2 to 1/4. This is a valid known-error incidence across all cuts, but not a measured probability of perceptually acceptable cuts. Unknown, confidently nonspeech, and safe linguistic boundaries need separate reporting; simply discarding all non-phone cuts would also be wrong.
3. Depth is `min(t - phone_start, phone_end - t)`, not word truncation or audible damage. A phone at most 100 ms long can never produce a >50 ms hit; a phone at most 200 ms long can never produce a >100 ms hit. Start/end direction and incomplete words are absent from these depth metrics.
4. Speech coverage is the union of retained phone duration. It rewards retaining both halves of a word across separate clips. It does not establish that either clip is independently usable.
5. Production `vr_slicer.py::candidate_clip_weight` uses duration alone. Once an acoustic cut passes detection, packing has no word/phrase completeness objective. Historical `safecut.py` instead searched gaps between aligned words with edge guards (defaults: 30 ms on each side plus an 80 ms usable gap). That structural difference is a plausible explanation for the listening preference, not proof of superiority.

The lab contract itself says outside a trusted phone means “not demonstrated unsafe,” not “safe.” The mistake is treating the limited metric as a product-quality acceptance test.

## Executed production diagnostic

Command:

```bash
workers/dataset/.venv/bin/python scripts/audit_slicer_referee.py
```

This loads the lab's frozen NeMo scopes and original audio for `s01/s0101b`, runs the current production `slice_wav`, then supplies annotations only to diagnostics. It does not feed labels to slicing.

| Measurement | Result |
|---|---:|
| Clips | 28 |
| Unique selected cuts | 55 |
| Inside trusted phones | 9 (16.36%) |
| Inside lexical words | 9 |
| >20 ms phone depth | 5 |
| >50 / >100 ms phone depth | 0 / 0 |
| Retained eligible speech | 62.18% |
| Outside per-buffer first-to-last phone span | 41 |
| Inside uncertainty masks | 0 |

Examples of cuts inside labeled words: `themselves` at 355.300 s, `definitely` at 435.488 s, `died` at 507.324 s. All nine word-interior cuts in this recording were also phone-interior cuts; the unpenalized inter-phone word split is a synthetic counterexample, not an observed claim about this recording.
The 41 outside-span cuts are not established errors: they may be legitimate silence. The phone-only referee does not distinguish that from missing evidence. One recording cannot establish corpus-level prevalence or subjective quality.

Outputs: `eval_runs/slicer_audit_2026-10-07/audit.json`, `selected_cut_audit.csv`, and `vr_result.json`. The script asserts the synthetic counterexamples before running VR. Production was not changed.

## Paired four-recording run

The comparison used the frozen Phase-4 smoke subset: `s01/s0101b`, `s02/s0201b`, `s03/s0301a`, `s04/s0401b`. Each recording was passed independently through production VR and the historical ASR+MFA+SafeCut worker. Both outputs were scored by the same Phase-1 referee against the same trusted Buckeye phones, uncertainty masks, and target buffers. The ASR+MFA worker saw its own faster-whisper transcript and MFA word alignment; neither slicer saw Buckeye reference labels.

The legacy worker was recovered from commit `be7b712^` into the experiment output overlay; production code was not changed. The old SSD's 5.5 GB `speechcraft-mfa` environment was made reachable at its former path with a symlink, and its cached Whisper `small.en` model was used. MFA 3.3.9 ran with `english_us_mfa` and `english_mfa`. Buckeye audio and annotations came from the existing accepted cohort.

| Recording | VR clips | VR coverage | ASR+MFA clips | ASR+MFA coverage |
|---|---:|---:|---:|---:|
| s0101b | 28 | 62.18% | 6 | 13.10% |
| s0201b | 9 | 49.17% | 3 | 20.21% |
| s0301a | 24 | 81.25% | 8 | 30.19% |
| s0401b | 38 | 57.73% | 7 | 9.31% |
| **Pooled** | **99** | **62.85%** | **24** | **15.90%** |

Phone-cut safety also fails to crown ASR+MFA here. Pooled, VR had 17/185 selected cuts inside trusted phones (9.19%); ASR+MFA had 5/45 (11.11%). VR had one cut deeper than 50 ms and none deeper than 100 ms. ASR+MFA had one >50 ms and one >100 ms. On `s0301a`, ASR+MFA had 3/14 inside-phone cuts and one >100 ms cut. These small denominators matter; the percentages are not stable corpus estimates.

This resolves the missing baseline question for the four-recording smoke subset, but it does **not** explain the user's listening preference or establish a winner. The old method kept far less speech and emitted fewer clips, while phone-based safety was similar and mixed. Better subjective clip boundaries remain plausible; the test still lacks word/phrase completeness and blind listening.

## Why ASR+MFA coverage is low

The missing coverage is mainly a pipeline yield problem, with a smaller denominator penalty from buffers that are too short for ASR+MFA:

- The scorer sees 743.25 seconds of eligible speech. ASR+MFA queues only buffers at least 3 seconds long; 200.18 seconds (26.9%) of eligible speech are in rejected shorter buffers. The reported 15.90% coverage becomes 21.76% when measured only against the 543.08 seconds ASR+MFA actually queues. The denominator explains 5.9 percentage points, not the remaining gap.
- The 94 queued buffers all received nonempty `small.en` transcripts and MFA TextGrids. MFA aligned 2,119 words; there were no missing TextGrids. One buffer was disabled by alignment QC for an absurdly long word, accounting for 30.49 seconds of eligible speech (4.1% of the total). Alignment failure is not the main cause.
- SafeCut examines 2,025 consecutive aligned-word boundaries. **1,815 have exactly zero gap**, 67 have a positive gap shorter than 140 ms, and 12 gaps of at least 140 ms are rejected by other checks. SafeCut requires 30 ms guards on each side and at least 80 ms remaining, so the raw gap must reach 140 ms. It accepts 131 cutpoints (6.47% of the examined boundaries). The summary's reported median gap is calculated from accepted cuts only; it hides how many aligned word pairs have no silence between them.
- The packer requires **two accepted interior cutpoints in the same buffer** to emit a legal 3–15 second clip. Of 94 queued buffers, 59 have fewer than two cuts. Another 14 have two or more but no legal 3–15 second pair. Only 21 buffers offer any candidate clip: 76 candidate pairs total. The non-overlap schedule keeps 24 clips, totaling 133.1 seconds of audio and 118.2 seconds of target speech.

So the old slicer is very conservative: it cuts only in pauses long enough to survive both word-edge guards and its RMS check. Its tastefulness is bought with sparse cutpoints. Requiring both clip edges to be such interior cuts further strands most speech. This is present in the historical Clip Lab assembler too: it explicitly rejects any buffer with fewer than two SafeCut points. The four-recording experiment therefore does not show a hidden MFA outage or reference-label leakage causing low coverage.

The detailed funnel is in `comparison.json` under `coverage_diagnosis`; each raw ASR, MFA, SafeCut, and packer artifact remains beside the run. A useful next experiment is to report coverage conditional on queued speech separately, then inspect whether safe buffer-edge endpoints or longer processing buffers can recover clips without cutting words. That change needs word-completeness review; merely counting buffer walls as safe would defeat the slicer's purpose.

## SafeCut threshold sweep

I regenerated only SafeCut candidates and repacked the cached ASR transcripts/MFA alignments for all four recordings. ASR, MFA, alignment QC, buffers, common packer, and Phase-1 referee were held fixed. The sweep tested 20 symmetric guard/gap combinations, 12 asymmetric combinations, and 25 guard distributions with a fixed 100 ms total raw-gap requirement. Full per-recording results and artifacts are under `eval_runs/slicer_head_to_head_2026-10-07/safecut_tuning*/results.json`; reproduce with `workers/dataset/.venv/bin/python scripts/tune_asr_mfa_safecut.py` and the `--asymmetric` / `--fixed100` modes.

| SafeCut setting (left / usable / right ms) | Coverage | Unique cuts inside phones | Clip edges inside phones | Cuts deeper than 50 ms | Clips |
| --- | ---: | ---: | ---: | ---: | ---: |
| Original 30 / 80 / 30 | 15.90% | 5/45 (11.11%) | 5/48 (10.42%) | 1 | 24 |
| 20 / 40 / 40 | 17.05% | 5/49 (10.20%) | 5/52 (9.62%) | 2 | 26 |
| Aggressive 0 / 0 / 0 | 20.67% | 12/61 (19.67%) | 12/64 (18.75%) | 2 | 32 |

The 20/40/40 setting is a modest candidate: it recovers 8.50 seconds of target speech on the pooled subset, with the same absolute number of cuts inside phones and a slightly lower inside-phone rate. It also adds one cut deeper than 50 ms. Nearly all recovered speech comes from `s0101b`; `s0201b` and `s0301a` are unchanged, while `s0401b` is essentially unchanged. The result is too concentrated in one recording and too small to call a general improvement. The low guards can raise coverage, but the aggressive setting materially increases cuts inside phones. No production defaults were changed; listen to the candidate clips and validate on a larger held-out recording set before adopting it.

Reproduce with `workers/dataset/.venv/bin/python scripts/run_asr_mfa_vr_comparison.py` while the SSD is mounted. Detailed pooled and per-recording referee output is in `eval_runs/slicer_head_to_head_2026-10-07/comparison.json`; full ASR, MFA, SafeCut, and scorer artifacts are under the same directory. The historical worker overlay is self-contained there. This remains a smoke test selected before the current comparison, not an untouched final test. Next, add a blinded listening check and explicit word/phrase completeness measures before any tuning or production decision.
