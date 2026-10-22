import assert from "node:assert/strict";
import test from "node:test";
import { completedAtStage, matchesWizardRun, wizardProcessingPlan, wizardProcessingStep } from "../src/components/wizard/processing-plan.mjs";
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
  assert.equal(plan.runQc, false);
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
  assert.equal(plan.runQc, false);
});

test("refresh during transcript QC waits for the existing job instead of resuming it", () => {
  const run = {
    status: "running",
    stage: "transcript_qc",
    input_summary: { single_speaker: true },
  };
  const plan = wizardProcessingPlan(run);
  assert.equal(plan.processClips, false);
  assert.equal(plan.resume, false);
  assert.equal(plan.runQc, false);
  assert.equal(plan.qcInProgress, true);
  assert.deepEqual(wizardProcessingStep(run), { kind: "wait", stage: "speaker_purity" });
});

test("a completed speaker_purity run opens the lab without restarting work", () => {
  for (const singleSpeaker of [true, false]) {
    const run = {
      id: "dataset-done",
      project_id: "project-1",
      status: "completed",
      stage: "speaker_purity",
      input_summary: { single_speaker: singleSpeaker },
    };
    const plan = wizardProcessingPlan(run);
    assert.equal(completedAtStage(run, "candidate_review_clips"), true);
    assert.equal(completedAtStage(run, "transcript_qc"), true);
    assert.equal(completedAtStage(run, "speaker_purity"), true);
    assert.equal(plan.processClips, false);
    assert.equal(plan.resume, false);
    assert.equal(plan.runQc, false);
    assert.equal(plan.start, false);
    assert.equal(plan.reviewReady, true);
    assert.deepEqual(wizardProcessingStep(run), { kind: "lab" });
  }
});

test("downstream completion covers earlier stages only after success", () => {
  const completedExport = { status: "completed", stage: "export" };
  assert.equal(completedAtStage(completedExport, "speaker_purity"), true);
  assert.equal(completedAtStage(completedExport, "candidate_review_clips"), true);
  assert.equal(completedAtStage({ status: "completed", stage: "native_export" }, "speaker_purity"), true);
  assert.equal(completedAtStage({ status: "completed", stage: "dataset_qc" }, "transcript_qc"), true);
  assert.equal(completedAtStage({ status: "running", stage: "speaker_purity" }, "candidate_clips"), true);
  assert.equal(completedAtStage({ status: "running", stage: "speaker_purity" }, "speaker_purity"), false);
  assert.equal(completedAtStage({ status: "completed", stage: "transcript_qc" }, "speaker_purity"), false);
  assert.equal(completedAtStage({ status: "completed", stage: "diarization" }, "candidate_review_clips"), false);
});

test("failed rejected and cancelled runs are not treated as successful stages", () => {
  for (const status of ["failed", "rejected", "cancelled"]) {
    const run = { status, stage: "speaker_purity", input_summary: { single_speaker: false } };
    assert.equal(completedAtStage(run, "candidate_review_clips"), false);
    assert.equal(completedAtStage(run, "speaker_purity"), false);
    const plan = wizardProcessingPlan(run);
    assert.equal(plan.resume, false);
    assert.equal(plan.processClips, false);
    assert.equal(plan.runQc, false);
    assert.equal(plan.reviewReady, false);
    assert.deepEqual(wizardProcessingStep(run), { kind: "error", reason: status });
  }
});

test("completed candidate clips still need QC and do not resume", () => {
  const run = { status: "completed", stage: "candidate_clips", input_summary: { single_speaker: false } };
  const plan = wizardProcessingPlan(run);
  assert.equal(plan.processClips, false);
  assert.equal(plan.resume, false);
  assert.equal(plan.runQc, true);
  assert.deepEqual(wizardProcessingStep(run), { kind: "qc" });
});

test("multi-speaker diarization completion resumes into slicing", () => {
  const run = { status: "completed", stage: "diarization", input_summary: { single_speaker: false } };
  assert.equal(wizardProcessingPlan(run).resume, true);
  assert.deepEqual(wizardProcessingStep(run), { kind: "clips", resume: true });
});

test("running diarization is not redirected to speaker selection", () => {
  const run = {
    status: "running",
    stage: "diarization",
    input_summary: { single_speaker: false },
    reason_codes: ["speaker_selection_required"],
  };
  assert.equal(wizardProcessingPlan(run).chooseSpeaker, false);
  assert.equal(wizardProcessingPlan(run).resume, false);
  assert.deepEqual(wizardProcessingStep(run), { kind: "clips", resume: false });
});

test("finished diarization that still needs a speaker opens choose-speaker", () => {
  const run = {
    status: "completed",
    stage: "diarization",
    input_summary: { single_speaker: false },
    reason_codes: ["speaker_selection_required"],
  };
  assert.equal(wizardProcessingPlan(run).chooseSpeaker, true);
  assert.equal(wizardProcessingPlan(run).resume, false);
  assert.equal(wizardProcessingPlan(run).processClips, false);
  assert.deepEqual(wizardProcessingStep(run), { kind: "choose-speaker" });
  const chosen = { selected: true, target_speaker_id: "speaker_1" };
  assert.equal(wizardProcessingPlan(run, chosen).chooseSpeaker, false);
  assert.deepEqual(wizardProcessingStep(run, chosen), { kind: "clips", resume: true });
});

test("single-speaker runs never resume for speaker selection", () => {
  const run = { status: "completed", stage: "diarization", input_summary: { single_speaker: true } };
  assert.equal(wizardProcessingPlan(run).resume, false);
  assert.deepEqual(wizardProcessingStep(run), { kind: "clips", resume: false });
});

test("a pending run starts once and does not resume", () => {
  const run = { status: "pending", stage: "ingest", input_summary: { single_speaker: false } };
  assert.deepEqual(wizardProcessingStep(run), { kind: "start" });
  assert.equal(wizardProcessingPlan(run).resume, false);
});

test("an unrecognized completed stage is an error instead of an endless wait", () => {
  const run = { status: "completed", stage: "not_a_stage", input_summary: { single_speaker: true } };
  assert.equal(completedAtStage(run, "candidate_review_clips"), false);
  assert.deepEqual(wizardProcessingStep(run), { kind: "error", reason: "unknown-stage" });
});
