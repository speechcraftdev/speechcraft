# Objective
Implement Phase 1 of the rebuilt Buckeye slicer benchmark in `buckeye-slicer-lab`: a small, independent referee layer with neutral data types, pure evaluation logic, and synthetic tests. This phase must not run or adapt any real slicer yet.

# What to implement
Create the smallest sensible module layout for these responsibilities (names may vary slightly if repo conventions strongly suggest better names, but keep it obvious):

1. Neutral benchmark data types
- `Cutpoint(recording_id, buffer_id, time_sec)`
- `Clip(recording_id, buffer_id, start_sec, end_sec)`
- `PhoneInterval(recording_id, buffer_id, start_sec, end_sec, phone)` or equivalent trusted target-speech interval type
- `UncertaintyInterval(recording_id, buffer_id, start_sec, end_sec)`
- `RecordingReference(recording_id, buffers, phones, uncertainty_intervals)` where buffers are immutable allowed scopes identified by `buffer_id`, `start_sec`, `end_sec`
- `SlicerResult(cutpoints, clips)`
- simple evaluator result/summary structures containing only the metrics below

Use frozen dataclasses or equally simple immutable structures where practical. Do not create a framework, registry, inheritance hierarchy, protocol zoo, or generic plugin API.

2. Pure evaluator
The evaluator must accept only `RecordingReference` + `SlicerResult` and must know nothing about VAD, RMS, geometry, thresholds, caches, detector internals, or Buckeye parsing machinery.

Compute:
- primary safety population = unique detector-selected internal cutpoints, deduplicated by `(recording_id, buffer_id, time_sec)`
- for each unique cutpoint, whether it is strictly inside a trusted target-speech phone interval
- phone-boundary semantics: for phone `[a, b]`, a cut at exactly `a` or exactly `b` is NOT inside; only `a < t < b` is inside
- if inside, `leak_depth_sec = min(t-a, b-t)`
- aggregate counts/rates for inside-phone, `>20 ms`, `>50 ms`, `>100 ms`
- secondary final-edge population from emitted clip starts/ends, preserving occurrences (do not deduplicate repeated edges unless the same literal clip edge is duplicated by duplicate clip rows, which should instead be rejected as invalid input)
- eligible target-speech duration = trusted target-phone duration inside accepted allowed buffers minus overlap with uncertainty masks
- retained target-speech duration = eligible trusted phone duration covered by the union of final emitted clips in the same `(recording_id, buffer_id)`
- speech coverage = retained / eligible, with a sensible explicit behavior when eligible duration is zero
- emitted audio duration = sum of emitted clip durations
- clip count

3. Validation/invariants
Fail clearly on malformed benchmark input instead of silently normalizing nonsense. At minimum validate:
- all interval starts < ends
- every buffer belongs to the recording and buffer ids are unique within the recording
- phone/uncertainty intervals reference existing buffer ids and remain within their buffer scope (allow tiny floating tolerance only if necessary, but prefer exact straightforward validation)
- cutpoints reference existing buffer ids and lie strictly inside the buffer (raw buffer edges are not legal detector cutpoints)
- clips reference existing buffer ids, stay within one buffer, satisfy `3.0 <= duration <= 15.0` seconds, and have start < end
- duplicate clip rows are rejected
- no cross-buffer identity collapse: all grouping/scoring keys include both recording and buffer identity

Do NOT add packing logic in Phase 1. The evaluator only validates/scorers supplied clips.

4. Interval arithmetic
Keep interval arithmetic boring and explicit. Implement only what the metrics need: intersection/union duration and subtracting uncertainty overlap from trusted phone intervals. Avoid external heavy dependencies unless already standard in the repo.

Be careful about double-counting:
- overlapping emitted clips must not double-count retained target speech; coverage must use the union of emitted clip coverage per buffer
- uncertainty masks may overlap each other; subtract their union, not the raw sum
- phone intervals should be treated as trusted target-speech intervals; if overlapping phone intervals exist unexpectedly, validation should reject or the coverage code must union them before duration math. Prefer rejecting impossible overlapping phones within a buffer if canonical Buckeye truth should never contain them, and document that assumption.

5. Synthetic tests — these are mandatory and should be comprehensive enough that Phase 1 can be reviewed once
Add focused unit tests independent of all real slicer code and corpus parsing.

Required tests:

A. Phone penetration exact semantics
Phone `[1.000, 1.100]` and cuts:
- 0.990 -> outside
- 1.000 -> outside
- 1.010 -> inside, depth 0.010
- 1.050 -> inside, depth 0.050
- 1.099 -> inside, depth 0.001
- 1.100 -> outside
Assert threshold counts so floating point comparisons around 20/50/100 ms are tested intentionally. Threshold semantics should be strict `>` as specified.

B. Coverage hand calculation
Trusted phones `[1,2]` and `[3,4]`; emitted clips `[0.5,1.5]` and `[3.2,3.8]`; expected retained target speech = `0.5 + 0.6 = 1.1 sec`, eligible = 2.0 sec, coverage = 0.55.
Use legal >=3 sec clips by embedding those intervals in a larger time axis if necessary; do not weaken the clip-duration invariant just to make the test convenient.

C. Uncertainty subtraction
At least one phone partially intersected by uncertainty, and overlapping uncertainty masks, proving only the union of uncertainty is removed from eligible and retained speech.

D. Buffer isolation regression
Two buffers with the same local timestamp values, e.g. both contain a cut at local/recording-relative `5.0`, and phones/clips in both. Prove scoring stays separate by `(recording_id, buffer_id)` and cannot reproduce the old global-local-time collapse bug.

E. Unique detector cuts vs final edge occurrences
Reuse one selected cutpoint as the end of one clip and start of another. Primary unique-cut safety counts it once; secondary final-edge population counts both occurrences.

F. Overlapping emitted clips
Two legal clips overlap the same trusted phone span. Retained speech must use union coverage and never exceed eligible duration.

G. Validation failures
Separate tests for malformed interval, unknown buffer id, cutpoint at raw buffer edge, clip shorter than 3 sec, clip longer than 15 sec, duplicate clip row, phone outside buffer, overlapping trusted phones if you choose rejection.

H. Zero eligible speech
Define and test explicit behavior. Prefer `speech_coverage = None` (or equivalent nullable value) rather than inventing 0 or 1, while still reporting eligible and retained seconds as zero. If you choose differently, document why.

6. Documentation
Add a short README section or small `PHASE1_REFEREE.md` explaining only:
- neutral contract: reference + cutpoints/clips -> metrics
- annotations are evaluator-only and are never passed to slicers
- primary unique cutpoints vs secondary final-edge occurrences
- outside-phone means `not demonstrated unsafe`, NOT `safe`
- no real slicer integration or caching exists in Phase 1
Keep this concise.

# Constraints
- This is a temporary research test that will run for days, then likely be abandoned. Reliability matters; extensibility does not.
- Keep implementation small and obvious. Prefer a few hundred lines total over infrastructure.
- No real slicer adapter in this phase.
- No VAD/RMS/geometry code.
- No cache code.
- No report generator, HTML, plots, CLI framework, plugin architecture, parameter sweep system, or giant metadata model.
- Do not duplicate or move canonical Buckeye datasets.
- Do not depend on production `speechcraft` internals in Phase 1.
- Testing logic must be structurally separate from future implementation-under-test logic.
- Persist nothing in Phase 1 unless a tiny fixture file is genuinely clearer than inline test data; prefer inline synthetic fixtures.

# Acceptance criteria
- All required synthetic tests pass.
- Evaluator can be understood without reading any slicer implementation.
- No module in Phase 1 imports real detector/slicer/VAD code.
- Coverage arithmetic is union-safe and uncertainty-union-safe.
- Buffer identity is present in every operation where timestamp collision could matter.
- Unique selected cutpoints and final edge occurrences are reported separately.
- Exact boundary and strict threshold semantics are tested.
- Malformed inputs fail loudly.
- Diff remains narrowly scoped to Phase 1; do not start Phase 2.
- Run the full relevant test suite and report exact commands/results.

# Out of scope
- A/D execution adapters
- geometry fingerprints
- feature or probability hashes
- cache regression implementation
- common packer implementation
- full Buckeye execution
- min_quiet_run_64ms / quiet_run_score
- real annotated TTS replay
- performance optimization
- cleanup/refactor of historical tournament code
