# Objective
Implement Phase 2 of the rebuilt Buckeye slicer benchmark in `buckeye-slicer-lab`, building directly on completed Phase 1. Also fix the one Phase-1 correctness issue found in review: exact strict leak-threshold comparisons must be numerically stable and tested through the evaluator itself.

# Phase-1 correctness fix (mandatory first)
The evaluator currently computes leak depth with floating-point subtraction and compares directly against strict thresholds (`>20 ms`, `>50 ms`, `>100 ms`). An intended exact threshold (for example 50 ms) can become `0.050000000000000044` and be incorrectly counted as greater than 50 ms.

Fix this narrowly and explicitly. Requirements:
- preserve strict mathematical semantics: exactly 20/50/100 ms is NOT counted as `>` that threshold;
- do not paper over this with tests of Python literals;
- add evaluator-level tests where phone/cut coordinates produce intended exact depths of 20 ms, 50 ms, and 100 ms;
- also test values just below and just above the thresholds so the comparison is not made too tolerant;
- use a simple, documented numeric policy (for example integer microseconds/milliseconds for threshold classification, or a tiny explicit comparison tolerance). Prefer the least surprising implementation;
- do not refactor unrelated Phase-1 code.

# Phase 2 goal
Add the minimal execution layer needed to run the two serious geometry baselines through the neutral Phase-1 contract:
- Current A baseline
- Proper D baseline

This phase is about execution/adaptation only. Do not add full-corpus orchestration, report generation, tournament machinery, or cache optimization.

# What to implement

## 1. Minimal slicer adapter boundary
Create the smallest obvious code unit that can invoke an existing/canonical slicer implementation for one recording and return the Phase-1 neutral `SlicerResult` only:
- `cutpoints`
- `clips`

The adapter input should be only what the slicer genuinely needs, such as:
- audio path or loaded mono audio (choose the simpler integration with existing production/snapshot code);
- sample rate if required;
- allowed buffers/scopes;
- explicit slicer configuration.

The adapter must NOT receive:
- phones;
- word annotations;
- uncertainty intervals;
- Buckeye reference labels;
- evaluator objects.

Make annotation ignorance structural, not merely documented.

## 2. Explicit immutable A and D configs
Represent A and D as explicit immutable configuration values, not magic branches hidden inside runner code.

A must correspond to the current overlapping geometry:
- window = 512 samples
- hop = 256 samples
- offsets = [0, 128]
- sample rate = 16000 Hz

D must correspond to the proper four-stream geometry:
- window = 512 samples
- hop = 512 samples
- offsets = [0, 128, 256, 384]
- sample rate = 16000 Hz

Include other detector parameters only if they are required to faithfully reproduce the existing `vad_percentile_rms` baseline behavior. Do not invent a giant general-purpose config model.

## 3. Reuse existing canonical detector/packer behavior
Use the existing production implementation or the closest trustworthy code snapshot already present in the repo rather than reimplementing VAD/RMS/packing from scratch.

Important:
- production code lives in `speechcraft`; copied lab code is historical/snapshot code;
- if direct import of `speechcraft` is practical and stable in this environment, prefer a very thin adapter around it;
- if the lab must use a snapshot because the production package is not importable, reuse the smallest existing canonical path and clearly label it as such;
- do not create separate "A benchmark detector" and "D benchmark detector" implementations. They should be the same execution path with different explicit geometry configs;
- use the same existing common 3–15 sec packer behavior for A and D where the canonical implementation already does so;
- do not add or modify packing logic in the evaluator.

## 4. Absolutely no shared acoustic feature cache in Phase 2
Given the prior invalid A-vs-D tournament result, correctness beats runtime.

For Phase 2:
- A computes its own VAD/features;
- D computes its own VAD/features;
- no cross-run reuse of VAD probabilities, frame arrays, RMS arrays, candidate arrays, or detector contexts;
- no global mutable detector context reused between A and D;
- no cache keyed only by recording or buffer;
- if the reused production implementation has unavoidable internal memoization, instantiate/execute it so A and D cannot share geometry-sensitive state. Document this in code/tests.

Do not implement a "better cache" yet. Caching belongs to a later phase only if needed.

## 5. Neutral output only
The public Phase-2 adapter result must remain Phase-1 `SlicerResult`.

Do not add VAD frames, RMS arrays, probability histories, quiet-run arrays, candidate explanations, or feature dictionaries to `SlicerResult`.

If a tiny internal execution diagnostic is absolutely necessary for tests, keep it private/separate and do not persist it as benchmark output.

## 6. Minimal tests
Add focused tests that prove adapter architecture and basic correctness without requiring the full Buckeye corpus.

Mandatory tests:

A. Config identity
- A and D configs are unequal;
- exact geometry fields match the values above;
- configs are immutable (or equivalently not accidentally mutated/shared).

B. Annotation ignorance
- the adapter API cannot accept/pass `PhoneInterval`, uncertainty masks, or a `RecordingReference` containing annotations into detector execution;
- a test using a fake/stub execution function is acceptable if needed to prove only audio/buffers/config cross the boundary.

C. Same path, different config
- prove A and D use the same adapter/execution path and differ by configuration rather than separate duplicated implementations.

D. No shared run state
- use a fake/stub feature/detector executor if necessary to prove two adapter invocations construct independent run state and do not reuse an acoustic cache/context;
- if using real production objects, prove separate instances are created per run/config.

E. Neutral result conversion
- given a small fake/canonical raw slicer output, conversion produces the exact expected Phase-1 `Cutpoint`/`Clip` values including correct `(recording_id, buffer_id)` identity.

F. Phase-1 evaluator compatibility
- pass an adapter-produced `SlicerResult` (fake execution is fine) into the real evaluator and verify scoring works without any implementation metadata.

G. Existing Phase-1 test suite continues to pass, including the new exact-threshold tests.

If a tiny real-audio smoke fixture already exists locally and can run cheaply without external downloads, one real A/D adapter smoke test is welcome. Do not make Phase 2 depend on downloading models/data or running the whole corpus.

## 7. Tiny documentation update
Update the existing Phase-1 referee documentation/README with only:
- Phase 2 adds A/D execution adapters;
- A and D share one adapter path and differ only by explicit geometry config;
- annotations remain evaluator-only;
- Phase 2 intentionally performs no shared acoustic caching.

Keep it short.

# Constraints
- This is a temporary research benchmark, not a reusable framework.
- Keep code boring and obvious.
- No CLI framework unless an existing tiny entry point is unavoidable; preferably none yet.
- No full Buckeye runner.
- No result persistence/report generation yet.
- No HTML, plots, parameter sweeping, registry/plugin system, generic dependency injection framework, or cache layer.
- No `min_quiet_run_64ms` or `quiet_run_score` yet.
- No geometry fingerprint/probability checksum regression suite yet; that is Phase 3.
- Do not alter the neutral evaluator contract to accommodate detector metadata.
- Do not modify production `speechcraft` code in this phase unless absolutely required; prefer lab-side adapters around existing code.
- Do not duplicate or move canonical datasets.

# Acceptance criteria
- Phase-1 exact-threshold bug is fixed with evaluator-level exact/below/above tests.
- A and D are represented by explicit immutable geometry configs with the exact agreed values.
- One common adapter/execution path runs either config.
- Detector execution receives no Buckeye annotation/reference labels.
- No shared acoustic cache/state exists between A and D runs.
- Public result is still only Phase-1 `SlicerResult` (cutpoints + clips).
- Focused adapter tests pass.
- Full relevant test suite passes and exact commands/results are reported.
- Diff stays narrowly within Phase 2; do not begin Phase 3 or full-corpus execution.

# Out of scope
- A/D geometry fingerprint regression and checksums (Phase 3)
- small historical-truth A/D corpus validation run (Phase 4)
- full Buckeye A/D run
- caching/performance optimization
- additional slicer contenders
- real annotated TTS replay
- benchmark reporting infrastructure
- historical tournament cleanup
