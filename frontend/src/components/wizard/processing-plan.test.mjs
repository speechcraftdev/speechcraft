import assert from "node:assert/strict";
import test from "node:test";
import {
  completedAtStage,
  DATASET_RUN_API_STOP_AFTER,
  WIZARD_INITIAL_STOP_AFTER,
  WIZARD_SELECTED_SPEAKER_STOP_AFTER,
  wizardResumeOptions,
  wizardRunOptions,
} from "./processing-plan.mjs";

test("wizard run creation uses a supported stop-after stage", () => {
  const request = wizardRunOptions();
  assert.equal(request.stop_after, "candidate_review_clips");
  assert.ok(DATASET_RUN_API_STOP_AFTER.includes(request.stop_after));
  assert.equal(WIZARD_INITIAL_STOP_AFTER, request.stop_after);
  assert.notEqual(request.stop_after, "alignment_qc");
});

test("speaker selection resumes at the supported candidate-clip stage", () => {
  const request = wizardResumeOptions();
  assert.equal(request.stop_after, "candidate_review_clips");
  assert.ok(DATASET_RUN_API_STOP_AFTER.includes(request.stop_after));
  assert.equal(WIZARD_SELECTED_SPEAKER_STOP_AFTER, request.stop_after);
  assert.notEqual(request.stop_after, "alignment_qc");
});

test("wizard rejects obsolete worker stages before sending an API request", () => {
  assert.throws(() => wizardRunOptions("alignment_qc"), /Unsupported dataset run stage/);
  assert.throws(() => wizardResumeOptions("alignment_qc"), /Unsupported dataset run stage/);
});

test("a later completed stage counts as earlier-stage success", () => {
  assert.equal(completedAtStage({ status: "completed", stage: "candidate_review_clips" }, "candidate_review_clips"), true);
  assert.equal(completedAtStage({ status: "running", stage: "candidate_review_clips" }, "candidate_review_clips"), false);
  assert.equal(completedAtStage({ status: "completed", stage: "speaker_purity" }, "candidate_review_clips"), true);
  assert.equal(completedAtStage({ status: "failed", stage: "speaker_purity" }, "candidate_review_clips"), false);
});
