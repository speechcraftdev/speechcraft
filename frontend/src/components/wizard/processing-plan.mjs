/** Stop-after stages accepted by the current dataset-run API. */
export const WIZARD_INITIAL_STOP_AFTER = "candidate_review_clips";
export const WIZARD_SELECTED_SPEAKER_STOP_AFTER = "candidate_review_clips";
export const DATASET_RUN_API_STOP_AFTER = Object.freeze([
  "source_audio",
  "audio_variants",
  "diarization",
  "buffers",
  "candidate_review_clips",
  "transcript_qc",
  "speaker_purity",
  "native_export",
]);

// The backend persists this stage as `candidate_clips`; the setup wizard and
// worker recipe call it `candidate_review_clips`.
const BACKEND_STAGE_ALIASES = Object.freeze({
  candidate_review_clips: "candidate_clips",
});

export function wizardRunOptions(stopAfter = WIZARD_INITIAL_STOP_AFTER) {
  if (!DATASET_RUN_API_STOP_AFTER.includes(stopAfter)) {
    throw new Error(`Unsupported dataset run stage: ${stopAfter}`);
  }
  return { stop_after: stopAfter };
}

export function wizardResumeOptions(stopAfter = WIZARD_SELECTED_SPEAKER_STOP_AFTER) {
  if (!DATASET_RUN_API_STOP_AFTER.includes(stopAfter)) {
    throw new Error(`Unsupported dataset run stage: ${stopAfter}`);
  }
  return { stop_after: stopAfter };
}

export function completedAtStage(run, stage) {
  return run?.status === "completed" &&
    (run?.stage === stage || run?.stage === BACKEND_STAGE_ALIASES[stage]);
}

/** Check that an existing run belongs to this onboarding attempt and exact input set. */
export function matchesWizardRun(run, { projectId, attemptId, recordingIds, model }) {
  const allowedStatuses = new Set(["pending", "running", "needs_review", "completed"]);
  const input = run?.input_summary ?? {};
  const actualIds = Array.isArray(input.source_recording_ids) ? [...input.source_recording_ids].sort() : [];
  const expectedIds = [...recordingIds].sort();
  return run?.project_id === projectId &&
    allowedStatuses.has(run?.status) &&
    input.wizard_attempt === attemptId &&
    input.single_speaker === false &&
    input.requested_stop_after === WIZARD_INITIAL_STOP_AFTER &&
    input.target_speaker_label === "speaker_0" &&
    input.language === "auto" &&
    (!model || input.whisper_model_size === model) &&
    expectedIds.length > 0 &&
    actualIds.length === expectedIds.length &&
    actualIds.every((id, index) => id === expectedIds[index]);
}
