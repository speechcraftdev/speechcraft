import assert from "node:assert/strict";
import test from "node:test";
import { completedAtStage, matchesWizardRun } from "../src/components/wizard/processing-plan.mjs";

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

const wizardRun = {
  project_id: "project-1",
  status: "pending",
  input_summary: {
    wizard_attempt: "login3:project-1:attempt-uuid",
    source_recording_ids: ["recording-a", "recording-b"],
    single_speaker: false,
    requested_stop_after: "candidate_review_clips",
    target_speaker_label: "speaker_0",
    language: "auto",
    whisper_model_size: "large-v3",
  },
};

const wizardRunIdentity = {
  projectId: "project-1",
  attemptId: "login3:project-1:attempt-uuid",
  recordingIds: ["recording-b", "recording-a"],
  model: "large-v3",
};

test("reuses a wizard run only for the exact attempt and recording set", () => {
  assert.equal(matchesWizardRun(wizardRun, wizardRunIdentity), true);
  assert.equal(
    matchesWizardRun(wizardRun, { ...wizardRunIdentity, recordingIds: ["recording-a"] }),
    false,
  );
  assert.equal(
    matchesWizardRun(wizardRun, { ...wizardRunIdentity, recordingIds: ["recording-a", "recording-c"] }),
    false,
  );
});

test("rejects a URL run from a different project or onboarding attempt", () => {
  assert.equal(matchesWizardRun(wizardRun, { ...wizardRunIdentity, projectId: "project-2" }), false);
  assert.equal(matchesWizardRun(wizardRun, { ...wizardRunIdentity, attemptId: "new-attempt" }), false);
});
