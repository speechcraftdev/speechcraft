# Phase 1 Referee

Small independent evaluation layer for slicer outputs. No real slicer runs here.

## Contract

```text
RecordingReference + SlicerResult(cutpoints, clips) -> EvaluationResult
```

`RecordingReference` carries allowed buffers plus evaluator-only annotations (trusted phones, uncertainty masks). Those annotations are never inputs to a slicer.

## Populations

- **Primary (unique cut safety):** detector-selected internal cutpoints, deduplicated by `(recording_id, buffer_id, time_sec)`.
- **Secondary (final-edge safety):** every emitted clip start and end, counted as occurrences (a shared boundary used as one clip’s end and another’s start counts twice).

Outside a trusted phone means **not demonstrated unsafe**, not “safe.”

## Phone / coverage rules

- Inside phone `[a, b]` only when `a < t < b` (exact endpoints are outside).
- Depth thresholds use strict `>` (20 / 50 / 100 ms).
- Overlapping trusted phones within one buffer are rejected (canonical Buckeye truth should not contain them).
- Eligible speech = trusted phones minus the **union** of uncertainty masks.
- Retained speech = eligible speech covered by the **union** of emitted clips per `(recording_id, buffer_id)`.
- If eligible duration is zero, `speech_coverage` is `None`.

## Phase 1 scope

Pure types, validation, interval arithmetic, and synthetic unit tests only. No slicer adapter, VAD/RMS/geometry, cache, packer, or report generator.
