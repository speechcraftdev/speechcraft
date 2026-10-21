const AUTH_ERRORS = new Set([
  "HF_TOKEN_INVALID",
  "HF_MODEL_ACCESS_REQUIRED",
  "HF_TOKEN_PERMISSION_DENIED",
  "HF_APPROVAL_PENDING",
  "AUTHORIZATION_REQUIRED_AFTER_RESTART",
]);

export function setupActionFor(status, models = []) {
  const job = status?.job || {};
  const pyannote = status?.components?.pyannote || models.find((model) => model.role === "diarization") || {};
  const needsAuthorization = job.state === "waiting_for_user"
    || pyannote.action_required === true
    || ["waiting_for_token", "access_denied"].includes(pyannote.state)
    || AUTH_ERRORS.has(job.error?.code);

  if (job.state === "running") return "watch";
  if (needsAuthorization) return "verify";
  if (job.state === "completed" && status?.technical_processing_ready === true) return "finalize";
  if (["failed", "cancelled", "interrupted"].includes(job.state)) return "recover";
  if (job.state === "completed") return "recover";
  if (["files_verified", "runtime_ready"].includes(pyannote.state)) return "download";
  return "verify";
}

export function downloadPercent(progress) {
  if (progress?.phase !== "downloading") return null;
  const indeterminate = progress.transfer_indeterminate || progress.indeterminate;
  if (!indeterminate) {
    const bytes = progress.downloaded_bytes;
    const totalBytes = progress.total_bytes;
    if (typeof bytes === "number" && typeof totalBytes === "number" && totalBytes > 0) {
      return Math.max(0, Math.min(100, Math.floor(bytes * 100 / totalBytes)));
    }
    const current = progress.current;
    const total = progress.total;
    if (typeof current === "number" && typeof total === "number" && total > 0) {
      return Math.max(0, Math.min(100, Math.floor(current * 100 / total)));
    }
  }
  const filesCompleted = progress.files_completed;
  const filesTotal = progress.files_total;
  if (Number.isSafeInteger(filesCompleted) && filesCompleted >= 0
      && Number.isSafeInteger(filesTotal) && filesTotal > 0) {
    return Math.max(0, Math.min(100, Math.floor(filesCompleted * 100 / filesTotal)));
  }
  return !indeterminate && typeof progress.percent === "number" && Number.isFinite(progress.percent)
    ? Math.max(0, Math.min(100, progress.percent)) : null;
}

export function setupFailureCodeFor(status) {
  const job = status?.job || {};
  if (job.state === "completed" && status?.technical_processing_ready !== true) return "PROCESSING_PREREQUISITES_MISSING";
  if (job.error?.code && job.error.code !== "SETUP_TASK_FAILED") return job.error.code;
  if (job.stage === "install_models") return "MODEL_DOWNLOAD_FAILED";
  if (["install_model_manager", "install_backend_environment", "install_worker_environment"].includes(job.stage)) return "PYTHON_RUNTIME_FAILED";
  if (["start_services", "verify_services"].includes(job.stage)) return "BACKEND_SERVICE_UNAVAILABLE";
  if (job.stage === "validate_runtime") return "PROCESSING_PREREQUISITES_MISSING";
  if (job.state === "interrupted") return "CONTROLLER_RESTARTED";
  return job.error?.code || "SETUP_TASK_FAILED";
}

/** @returns {{ category: string, message: string, action: "retry" | "verify" }} */
export function setupFailureFor(code, message = "") {
  if (["HF_TOKEN_INVALID", "HF_TOKEN_PERMISSION_DENIED"].includes(code)) {
    return { category: "hugging_face_token", message: "Hugging Face rejected this token. Check that it has read access, then verify again.", action: "verify" };
  }
  if (["HF_MODEL_ACCESS_REQUIRED", "HF_APPROVAL_PENDING"].includes(code)) {
    return { category: "model_conditions", message: "Accept the pyannote Community-1 model conditions and confirm account access, then verify again.", action: "verify" };
  }
  if (code === "NETWORK_UNAVAILABLE" || /NETWORK|FETCH_FAILED/.test(code || "") || /network (is )?unavailable/i.test(message)) {
    return { category: "network", message: "The network is unavailable. Check the connection and retry.", action: "retry" };
  }
  if (code === "HF_NETWORK_ERROR") return { category: "network", message: "Could not reach Hugging Face. Check the network connection and retry.", action: "retry" };
  if (["CONTROLLER_UNAVAILABLE", "CONTROLLER_STOPPING", "SETUP_CONTROLLER_UNAVAILABLE"].includes(code)) {
    return { category: "setup_controller", message: "The setup controller is unavailable. Restart Speechcraft, then retry.", action: "retry" };
  }
  if (["BACKEND_UNAVAILABLE", "BACKEND_RESTORE_FAILED", "BACKEND_HEALTH_FAILED", "BACKEND_SERVICE_UNAVAILABLE"].includes(code)) {
    return { category: "backend", message: "The processing backend is unavailable. Retry setup so Speechcraft can restore it.", action: "retry" };
  }
  if (code === "MODEL_INITIALIZATION_FAILED") {
    return { category: "model_initialization", message: "The installed Whisper model could not initialize on this machine. Check available memory and supported hardware, then retry setup.", action: "retry" };
  }
  if (["RUNTIME_PROBE_ENVIRONMENT_UNAVAILABLE", "PROCESSING_PREREQUISITES_MISSING"].includes(code)) {
    return { category: "prerequisites", message: "The managed worker or another processing requirement is not verified yet. Retry setup to verify or repair it.", action: "retry" };
  }
  if (/PYTHON|PACKAGE|DEPENDENCY|WORKER_ENVIRONMENT|BACKEND_ENVIRONMENT/.test(code || "")) {
    return { category: "python_runtime", message: "Speechcraft could not install or verify its managed Python runtime. Retry setup; if it fails again, repair the installation.", action: "retry" };
  }
  if (/MODEL.*(VERIFY|INTEGRITY|CORRUPT)|INTEGRITY|MANIFEST/.test(code || "")) {
    return { category: "model_verification", message: "An installed model did not pass its integrity check. Retry setup to verify or repair it.", action: "retry" };
  }
  if (/MODEL.*(DOWNLOAD|INSTALL)|DOWNLOAD.*MODEL|HF_SNAPSHOT/.test(code || "")) {
    return { category: "model_download", message: "A model download failed. Check the network and retry; verified files will be reused.", action: "retry" };
  }
  if (["CONTROLLER_RESTARTED", "BOOTSTRAP_INTERRUPTED", "AUTHORIZATION_REQUIRED_AFTER_RESTART"].includes(code)) {
    return { category: "interrupted", message: "Setup stopped before it finished. Verify Hugging Face access again if requested, then retry setup.", action: "retry" };
  }
  if (code === "PROCESSING_PREREQUISITES_MISSING" || /prerequisites are not ready/i.test(message)) {
    return { category: "prerequisites", message: "Setup finished, but some processing requirements are still missing. Retry setup to repair them.", action: "retry" };
  }
  return { category: "setup", message: message || "Setup could not finish. Retry to continue from the existing installation.", action: "retry" };
}
