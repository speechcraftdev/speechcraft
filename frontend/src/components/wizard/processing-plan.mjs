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

/**
 * Pipeline order shared by the dataset worker and the persisted run stage.
 * Names in the same group are aliases for one stage. A later group is
 * downstream of every earlier group.
 *
 * The backend stores `candidate_clips` where the wizard says
 * `candidate_review_clips`, and `export` where the worker says `native_export`.
 */
const STAGE_GROUPS = Object.freeze([
  ["ingest", "source_audio"],
  ["audio_variants"],
  ["source_health"],
  ["vad"],
  ["diarization"],
  ["speaker_identity"],
  ["trusted_regions"],
  ["processing_buffers", "buffers", "asr_queue"],
  ["asr"],
  ["normalization"],
  ["mfa", "alignment_qc"],
  ["safe_cutpoints"],
  ["candidate_clips", "candidate_review_clips"],
  ["transcript_qc"],
  ["speaker_purity"],
  ["dataset_qc"],
  ["export", "native_export"],
]);

const STAGE_RANK = new Map(STAGE_GROUPS.flatMap((names, rank) => names.map((name) => [name, rank])));
const TERMINAL_FAILURE = new Set(["failed", "rejected", "cancelled"]);
const QC_STAGES = new Set(["transcript_qc", "speaker_purity"]);
const SPEAKER_SELECTION_REQUIRED = "speaker_selection_required";

export function stageRank(stage) {
  return STAGE_RANK.has(stage) ? STAGE_RANK.get(stage) : null;
}

/**
 * A stage is complete only when the run state shows it finished successfully.
 *
 * - `completed` covers the current stage and every earlier stage.
 * - `running` or `needs_review` covers only stages strictly before the current one.
 * - `failed`, `rejected`, and `cancelled` never count as success, even when the
 *   stage label has moved forward.
 */
export function completedAtStage(run, stage) {
  const target = stageRank(stage);
  const current = stageRank(run?.stage);
  if (target == null || current == null) return false;
  if (TERMINAL_FAILURE.has(run?.status)) return false;
  if (run?.status === "completed") return current >= target;
  if (run?.status === "running" || run?.status === "needs_review") return current > target;
  return false;
}

export function speakerSelectionRequired(run) {
  const codes = [
    ...(Array.isArray(run?.reason_codes) ? run.reason_codes : []),
    ...(Array.isArray(run?.output_summary?.reason_codes) ? run.output_summary.reason_codes : []),
  ];
  return codes.includes(SPEAKER_SELECTION_REQUIRED);
}

/**
 * Multi-speaker diarization has finished and the target voice is not chosen.
 * A running diarization stage is still working, so it is not this state.
 * A saved selection wins over a stale reason code.
 */
export function awaitsSpeakerSelection(run, selection) {
  if (Boolean(run?.input_summary?.single_speaker)) return false;
  if (run?.status === "running" || run?.status === "pending") return false;
  if (TERMINAL_FAILURE.has(run?.status)) return false;
  if (run?.stage !== "diarization") return false;
  if (completedAtStage(run, "candidate_review_clips")) return false;
  if (selection?.selected === true && selection?.target_speaker_id) return false;
  if (selection?.selected === false) return true;
  return speakerSelectionRequired(run) && (run?.status === "completed" || run?.status === "needs_review");
}

/** Decide the frontend work after a run is loaded, without invoking invalid endpoints. */
export function wizardProcessingPlan(run, selection) {
  const status = run?.status;
  const singleSpeaker = Boolean(run?.input_summary?.single_speaker);
  const terminalFailure = TERMINAL_FAILURE.has(status);
  const knownStage = stageRank(run?.stage) != null;
  const clipsComplete = completedAtStage(run, "candidate_review_clips");
  const qcComplete = completedAtStage(run, "speaker_purity");
  const qcInProgress = status === "running" && QC_STAGES.has(run?.stage);
  const chooseSpeaker = awaitsSpeakerSelection(run, selection);
  return {
    singleSpeaker,
    terminalFailure,
    knownStage,
    chooseSpeaker,
    start: status === "pending",
    resume: !chooseSpeaker && !singleSpeaker && !clipsComplete && !terminalFailure && knownStage && status !== "running" && status !== "pending",
    processClips: !chooseSpeaker && !terminalFailure && !clipsComplete && knownStage && status !== "pending",
    runQc: !terminalFailure && clipsComplete && !qcComplete && !qcInProgress,
    qcInProgress,
    reviewReady: !terminalFailure && qcComplete,
  };
}

/**
 * The single next action for the processing page. Callers re-read the run
 * after `start`, `clips`, `wait`, or `qc` and ask again. A completed downstream
 * stage therefore goes to the lab without resume, restart, or a second QC launch.
 */
export function wizardProcessingStep(run, selection) {
  const plan = wizardProcessingPlan(run, selection);
  if (!run || typeof run !== "object") return { kind: "error", reason: "missing-run" };
  if (plan.terminalFailure) return { kind: "error", reason: run.status };
  if (plan.start) return { kind: "start" };
  if ((run.status === "completed" || run.status === "running" || run.status === "needs_review") && !plan.knownStage) {
    return { kind: "error", reason: "unknown-stage" };
  }
  if (plan.chooseSpeaker) return { kind: "choose-speaker" };
  if (plan.reviewReady) return { kind: "lab" };
  if (plan.qcInProgress) return { kind: "wait", stage: "speaker_purity" };
  if (plan.processClips) return { kind: "clips", resume: plan.resume };
  if (plan.runQc) return { kind: "qc" };
  return { kind: "error", reason: "stuck" };
}

/** Check that an existing run belongs to this onboarding attempt and exact input set. */
export function matchesWizardRun(run, { projectId, attemptId, recordingIds, model, singleSpeaker = false }) {
  const allowedStatuses = new Set(["pending", "running", "needs_review", "completed"]);
  const input = run?.input_summary ?? {};
  const actualIds = Array.isArray(input.source_recording_ids) ? [...input.source_recording_ids].sort() : [];
  const expectedIds = [...recordingIds].sort();
  return run?.project_id === projectId &&
    allowedStatuses.has(run?.status) &&
    input.wizard_attempt === attemptId &&
    input.single_speaker === singleSpeaker &&
    input.requested_stop_after === WIZARD_INITIAL_STOP_AFTER &&
    input.target_speaker_label === "speaker_0" &&
    input.language === "auto" &&
    (!model || input.whisper_model_size === model) &&
    expectedIds.length > 0 &&
    actualIds.length === expectedIds.length &&
    actualIds.every((id, index) => id === expectedIds[index]);
}

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
