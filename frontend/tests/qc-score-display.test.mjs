import assert from "node:assert/strict";
import test from "node:test";
import { formatNullableQcScore } from "../src/components/lab/qc-score-display.mjs";
import { DEFAULT_SORT_DIRECTIONS, filterClips, SORT_OPTIONS, sortClips } from "../src/components/lab/lab-data.ts";

const formatScore = (score) => score.toFixed(2);

test("nullable QC scores display as unscored rather than a fabricated zero", () => {
  assert.equal(formatNullableQcScore(null, formatScore), "Not scored");
  assert.equal(formatNullableQcScore(undefined, formatScore), "Not scored");
});

test("actual zero and nonzero QC scores remain numeric", () => {
  assert.equal(formatNullableQcScore(0, formatScore), "0.00");
  assert.equal(formatNullableQcScore(97.4519, formatScore), "97.45");
});

const clip = (id, order, { tc = null, sp = null, rq = null, duration = null, status = "unresolved", transcript = id } = {}) => ({
  id, order, transcript, originalTranscript: transcript, durationSeconds: duration ?? 0,
  effectiveDurationRaw: duration, status, machineBucket: "needs_review", qcScore: 0.5,
  transcriptConfidence: tc ?? 0, speakerPurity: sp ?? 0, transcriptMatchRaw: tc,
  speakerCheckRaw: sp, referenceQuality: rq, variant: "source", reasonCodes: [], peaks: [],
  sampleRateHz: 16000, channels: 1, speaker: "speaker", language: "en", sourceRecording: "source",
  originalStartSeconds: 0, originalEndSeconds: duration ?? 0, revisions: [], edits: [],
});

test("Clip Lab exposes exactly the five requested sort options", () => {
  assert.deepEqual(SORT_OPTIONS.map(({ label }) => label), [
    "Source Order", "Transcript Confidence", "Speaker Purity", "Duration", "Reference Quality",
  ]);
});

test("every sort method starts with its downward direction", () => {
  assert.deepEqual(Object.values(DEFAULT_SORT_DIRECTIONS), ["down", "down", "down", "down", "down"]);
});

test("metric sorts work in both directions, preserve source order on ties, and keep missing last", () => {
  const clips = [
    clip("tie-later", 4, { tc: 90, sp: 0, rq: 60, duration: 4 }),
    clip("zero", 2, { tc: 0, sp: 80, rq: 0, duration: 11 }),
    clip("tie-earlier", 1, { tc: 90, sp: 80, rq: 60, duration: 11 }),
    clip("missing", 3, { duration: null }),
  ];
  const inputOrder = [...clips];
  assert.deepEqual(sortClips(clips, "source").map((row) => row.id), ["tie-earlier", "zero", "missing", "tie-later"]);
  assert.deepEqual(sortClips(clips, "source", "up").map((row) => row.id), ["tie-later", "missing", "zero", "tie-earlier"]);
  assert.deepEqual(sortClips(clips, "transcript_confidence").map((row) => row.id), ["tie-earlier", "tie-later", "zero", "missing"]);
  assert.deepEqual(sortClips(clips, "transcript_confidence", "up").map((row) => row.id), ["zero", "tie-earlier", "tie-later", "missing"]);
  assert.deepEqual(sortClips(clips, "speaker_purity").map((row) => row.id), ["tie-earlier", "zero", "tie-later", "missing"]);
  assert.deepEqual(sortClips(clips, "speaker_purity", "up").map((row) => row.id), ["tie-later", "tie-earlier", "zero", "missing"]);
  assert.deepEqual(sortClips(clips, "reference_quality").map((row) => row.id), ["tie-earlier", "tie-later", "zero", "missing"]);
  assert.deepEqual(sortClips(clips, "reference_quality", "up").map((row) => row.id), ["zero", "tie-earlier", "tie-later", "missing"]);
  assert.deepEqual(sortClips(clips, "duration").map((row) => row.id), ["tie-earlier", "zero", "tie-later", "missing"]);
  assert.deepEqual(sortClips(clips, "duration", "up").map((row) => row.id), ["tie-later", "tie-earlier", "zero", "missing"]);
  assert.deepEqual(clips, inputOrder);
});

test("sorting after search and status filters retains the matching subset", () => {
  const clips = [
    clip("a", 1, { tc: 20, transcript: "keep this", status: "unresolved" }),
    clip("b", 2, { tc: 95, transcript: "keep this", status: "accepted" }),
    clip("c", 3, { tc: 80, transcript: "other", status: "unresolved" }),
  ];
  const filtered = filterClips(clips, "keep", ["unresolved"]);
  assert.deepEqual(sortClips(filtered, "transcript_confidence").map((row) => row.id), ["a"]);
});
