import assert from "node:assert/strict";
import test from "node:test";
import { completedAtStage, matchesWizardRun, wizardProcessingPlan } from "../src/components/wizard/processing-plan.mjs";
import { LEGACY_ONBOARDING_ROUTES, legacyOnboardingRedirect } from "../src/lib/onboarding-routes.mjs";

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

test("speaker mode is part of run reuse identity", () => {
  const singleRun = {
    ...wizardRun,
    input_summary: { ...wizardRun.input_summary, single_speaker: true },
  };
  assert.equal(matchesWizardRun(singleRun, { ...wizardRunIdentity, singleSpeaker: true }), true);
  assert.equal(matchesWizardRun(singleRun, { ...wizardRunIdentity, singleSpeaker: false }), false);
});

test("legacy onboarding routes point to descriptive pages and retain query values", () => {
  assert.deepEqual(LEGACY_ONBOARDING_ROUTES, {
    "/login": "/onboarding/welcome",
    "/login2": "/onboarding/import",
    "/login3": "/onboarding/setup",
    "/login4": "/onboarding/choose-speaker",
    "/login5": "/onboarding/processing",
    "/login6": "/lab",
  });
  assert.equal(
    legacyOnboardingRedirect("/login5", { project: "project one", run: "run-1", demo: "1" }),
    "/onboarding/processing?project=project+one&run=run-1&demo=1",
  );
});

test("single-speaker runs skip manual selection resume and keep QC enabled", () => {
  const plan = wizardProcessingPlan({
    status: "running",
    input_summary: { single_speaker: true },
    stage: "diarization",
  });
  assert.equal(plan.singleSpeaker, true);
  assert.equal(plan.resume, false);
  assert.equal(plan.processClips, true);
  assert.equal(plan.runQc, true);
});

test("multi-speaker runs resume after selection and retain QC", () => {
  const plan = wizardProcessingPlan({
    status: "needs_review",
    input_summary: { single_speaker: false },
    stage: "diarization",
  });
  assert.equal(plan.singleSpeaker, false);
  assert.equal(plan.resume, true);
  assert.equal(plan.processClips, true);
  assert.equal(plan.runQc, true);
});

test("refresh during transcript QC waits for the existing job instead of resuming it", () => {
  const plan = wizardProcessingPlan({
    status: "running",
    stage: "transcript_qc",
    input_summary: { single_speaker: true },
  });
  assert.equal(plan.processClips, true);
  assert.equal(plan.resume, false);
  assert.equal(plan.qcInProgress, true);
});
