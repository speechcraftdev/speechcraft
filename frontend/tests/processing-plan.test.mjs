import assert from "node:assert/strict";
import test from "node:test";
import { completedAtStage } from "../src/components/wizard/processing-plan.mjs";

test("recognizes the backend candidate_clips spelling for wizard review completion", () => {
  assert.equal(
    completedAtStage({ status: "completed", stage: "candidate_clips" }, "candidate_review_clips"),
    true,
  );
});

test("continues to recognize the wizard candidate review spelling", () => {
  assert.equal(
    completedAtStage({ status: "completed", stage: "candidate_review_clips" }, "candidate_review_clips"),
    true,
  );
});

test("does not treat an incomplete candidate stage as completed", () => {
  assert.equal(
    completedAtStage({ status: "running", stage: "candidate_clips" }, "candidate_review_clips"),
    false,
  );
});
