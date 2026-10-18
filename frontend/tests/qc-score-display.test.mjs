import assert from "node:assert/strict";
import test from "node:test";
import { formatNullableQcScore } from "../src/components/lab/qc-score-display.mjs";

const formatScore = (score) => score.toFixed(2);

test("nullable QC scores display as unscored rather than a fabricated zero", () => {
  assert.equal(formatNullableQcScore(null, formatScore), "Not scored");
  assert.equal(formatNullableQcScore(undefined, formatScore), "Not scored");
});

test("actual zero and nonzero QC scores remain numeric", () => {
  assert.equal(formatNullableQcScore(0, formatScore), "0.00");
  assert.equal(formatNullableQcScore(97.4519, formatScore), "97.45");
});
