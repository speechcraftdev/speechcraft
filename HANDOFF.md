# Handoff: can we ship frozen O0_4?

**Review branch:** `linus/attempt-3`  
**Commits:** `d71fb74` (Phase 10B negative LR result), `ae4b948` (Phase 10C signed-margin + score-control)  
**Question:** freeze/ship **O0_4**, or keep hunting a learned reranker?

Read this file, the 10B/10C summaries, **and** the Linus chats listed below. Those chats are part of the review, not optional background.

## Also read: Linus chats (ChatGPT + this Cursor thread)

These are design/review threads with **Linus**, our code reviewer. Please open them before deciding.

**Shared ChatGPT** (Aarav + you already have access). Look up these two chats by title:

1. **Slicer test reimplementation** — how the slicer test / Buckeye lab was rebuilt and what Linus required.
2. **Speaker purity score design** — how speaker-purity scoring was designed and reviewed.

They live in the shared ChatGPT workspace. If a share link is missing, ask Aarav; do not skip them.

**This Cursor chat** (O0_4 geometry through Phase 10C, including Linus’s 10B/10C reviews and the agent work):

```text
handoff/cursor_chat_linus_attempt3.md
```

## Proposed decision

**Ship frozen O0_4. Do not ship logistic rerankers or signed-margin rerankers.**

O0_4 geometry fingerprint (must not change):

```text
87130029b5443647ed1f1febd32ab768bf957a0a1698217eb3cf14b2d00c6ecf
```

Cohort fingerprint (120 recordings, 23 speakers):

```text
f3177a2fd9b69434a0cc91856bd13da45d9b0a1f4a8a6b240b7fa16e213644dd
```

O0_4 packing is duration-only. Detector `original_score` does **not** enter clip weights unless a later policy adds it. `scoring=None`.

## What to read

| File | Why |
|---|---|
| Shared ChatGPT: **Slicer test reimplementation** | Linus + Aarav design/review. Required reading. |
| Shared ChatGPT: **Speaker purity score design** | Linus + Aarav design/review. Required reading. |
| `handoff/cursor_chat_linus_attempt3.md` | This Cursor thread (O0_4 through 10C) |
| `phase10b_logistic_full/summary.json` | 120-recording LR negative result |
| `phase10c_margin_full/summary.json` | 120-recording signed-margin + O0_4 score-control |
| `phase10c_margin_full/summary.json` → `label_reasons` | Why ~70% of candidates stay unlabeled |
| `handoff/cohort_recordings.txt` | The 120 recording IDs |
| `adapter/config.py` | Frozen `O0_4` knobs |

Lab tests (no Buckeye audio):

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

## Phase 10B (logistic)

Speaker-held-out LR on frozen 15 features ranked known bad vs safe well (`ALL × inside` ROC-AUC 0.854). Packed schedules did not beat O0_4: every LR variant lost coverage and/or worsened >20/>50/>100.

Do not ship those rerankers. Do not retune C / class weights / calibration on that label formulation.

## Phase 10C (signed margin)

Target: negative = inside-phone penetration, 0 = phone edge, positive = distance to nearest phone, cap ±100 ms. Train on usable rows inside trusted annotation coverage; predict every candidate. One `LinearRegression`, speaker-held-out OOF.

**Unlabeled pool (120 recordings, 29,912 candidates):**

| reason | count |
|---|---:|
| unknown_before_annotated_span | 9,332 |
| unknown_after_annotated_span | 11,113 |
| unknown_no_phones | 571 |
| unknown_uncertainty | 7 |
| unknown_outside_buffer | 0 |
| **unknown total** | **21,023 (70.3%)** |
| usable_inside / edge / outside | 3,340 / 6 / 5,543 |
| **usable total** | **8,889 (29.7%)** |

Signed margin trains on the near-phone middle *inside first-phone→last-phone coverage*. It does not train on most of the detector pool.

**O0_4 score-control** (full candidate set; `weight += score_weight × (start_score + end_score)`):

Turning the existing detector score **up** raises coverage and **worsens** deep-cut rates. That is a yield axis, not a safety frontier. `keep90…keep50` is candidate pruning and is **not** the O0_4 frontier.

| point | coverage | >20 | >50 | >100 |
|---|---:|---:|---:|---:|
| **O0_4 (score_weight=0)** | **65.24%** | **4.14%** | **0.99%** | **0.19%** |
| sw1 | 65.43% | 4.19% | 1.02% | 0.17% |
| sw4 | 65.91% | 4.37% | 1.16% | 0.22% |
| keep80 (pruning diagnostic) | 63.90% | 4.29% | 1.08% | 0.22% |
| MARGIN s0.5 | 65.17% | 4.06% | 0.97% | 0.18% |
| MARGIN s1 | 64.89% | 4.18% | 1.02% | 0.20% |

OOF on 8,889 usable candidates: Spearman 0.57, MAE 30 ms.

No learned point is better on coverage **and** safety. `s0.5` is a 0.07 pp coverage drop with a small safety tick; it is not a production reranker.

## What is in this git repo

- Referee, adapter, tests, phase scripts
- Compact 10B/10C result tables (`summary.json`, `recordings.csv`, OOF preds)
- This handoff, the 120 recording IDs, and the Cursor chat export

**Not in git (local symlinks only):**

```text
canonical/normalized      -> Buckeye reviewed phones / uncertainty
canonical/acoustic_matrix -> cohort + NeMo eval_runs
canonical/nemo
```

Wavs are gitignored. On the original machine they live under `/home/aaravthegreat/Datasets/buckeye/` (~2.6 GB raw wav, ~160 MB normalized annotations).

## Buckeye audio / annotations — not redistributed here

The Buckeye Corpus is a licensed speech dataset. This repo does **not** contain the wavs or the full normalized annotation tree. Do not copy them into git.

For the **ship decision**, audio is not required: the 120-recording metrics are already in `phase10c_margin_full/summary.json`.

To **reproduce** a Silero/packer run you need a local Buckeye checkout plus `speaker_ts_eval`, then point the `canonical/*` symlinks at it. The 120 IDs are in `handoff/cohort_recordings.txt`.

## Extra local branches

| branch | notes |
|---|---|
| `linus/attempt-3` | Review this. 10B + 10C. |
| `linus/attempt-2` | Older attempt. |
| `master` | Older lab index. |

Uncommitted leftovers on the original working tree (Phase 9 OpenVPI, shards, 23 MB `candidates.csv`) are **not** part of this handoff.
