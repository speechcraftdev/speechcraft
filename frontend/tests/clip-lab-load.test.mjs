import assert from "node:assert/strict";
import test from "node:test";
import { clipLabFailurePresentation } from "../src/components/lab/clip-lab-load.mjs";

test("a missing candidate manifest is an actionable Clip Lab error", () => {
  const presentation = clipLabFailurePresentation({
    status: 400,
    detail: "artifacts/candidate_review_manifest.json is missing",
  });
  assert.equal(presentation.title, "Processed clips are not available");
  assert.equal(presentation.message, "artifacts/candidate_review_manifest.json is missing");
  assert.equal(presentation.action, "speakers");
  assert.notEqual(presentation.title, "Backend unavailable");
});

test("a transport failure stays retryable and is not described as ready", () => {
  const presentation = clipLabFailurePresentation({ status: 0, detail: "" });
  assert.equal(presentation.action, "retry");
  assert.match(presentation.title, /could not be loaded/i);
});
