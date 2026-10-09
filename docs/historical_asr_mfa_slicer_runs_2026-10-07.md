# Historical ASR+MFA slicer outputs

## What was found

The current checkout contains earlier `fullwav-slicer` exports for four dataset bundles. These look like repeated versions of the old ASR+MFA slicing workflow: slices include aligned word transcripts/confidence, `safe_gap` boundary types, and per-export coverage/yield statistics. The Emma set has five saved revisions over the same nine input chunks; those are iterations, not five independent datasets.

| Dataset bundle / saved revision | Inputs | Clips | Source audio | Aligned span | Unique covered audio | Covered / source | Flagged clips |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Emma, Phase 3 | 9 chunks | 188 | 24.32 min | 24.16 min | 24.16 min | 99.33% | 73 (38.8%) |
| Billie (HeyBillieRae) | 1 | 119 | 15.67 min | 15.66 min | 15.66 min | 99.94% | 38 (31.9%) |
| Pokimane | 1 | 254 | 33.06 min | 33.03 min | 33.03 min | 99.92% | 99 (39.0%) |
| Sydney Sweeney | 1 | 149 | 18.50 min | 18.50 min | 18.50 min | 99.96% | 13 (8.7%) |
| **Pooled (one saved revision per bundle)** | **12 inputs** | **710** | **91.55 min** | **91.34 min** | **91.34 min** | **99.77%** | **223 (31.4%)** |

The pooled output is about **7.76 clips per source minute**, with a mean clip duration of **7.72 seconds**. The saved outputs report zero forced cuts. We independently intersected the saved training-clip intervals with MFA word intervals for Billie, Pokimane, and Sydney: all positive-duration aligned word time was covered. That is consistent with a full-recording tiler that aims to retain nearly all aligned speech.

## What these numbers do and do not say

These archived runs demonstrate high **MFA-aligned word coverage** and provide substantial clip-yield evidence across datasets. They do not contradict the 15.90% Phase-1-referee result from the four-recording Buckeye comparison. The earlier exports slice complete recordings into mostly contiguous clips; the Buckeye comparison counts only eligible target speech retained inside clips packed from raw speech buffers. Those denominators answer different questions. The historical exports also lack the same trusted phone reference and uncertainty masks, so their `flagged_clips` field is a slicer heuristic, not the referee's inside-phone rate.

Emma's saved versions range from 188 to 189 clips and roughly 99.33–99.50% source coverage, while the flagged share swings from 0% in Phase 1 to 38.8% in Phase 3. That is a useful warning: coverage barely moves across iterations while the edge-quality heuristic changes substantially. The run labels encode development revisions, not a held-out benchmark.

## Old SSD access

The old SSD is present as Btrfs UUID `2106f036-64ca-4106-8672-f466957a698a` (`/dev/nvme1n1p2`), but it is not mounted in this session. UDisks cannot connect to a daemon here, and a read-only mount attempt failed because this container lacks superuser privileges. Therefore this report covers historical exports already in the current checkout; it cannot claim to have inventoried additional files that exist only on the old SSD.

Source artifacts are under `backend/exports/{emma,heybillierae,pokimane,sydneysweeney}-fullwav-slicer*` and the matching `folder-asr-align` directories.
