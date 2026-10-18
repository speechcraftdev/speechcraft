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
