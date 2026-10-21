function asrState(preflight) {
  const asr = preflight?.asr_model;
  if (!asr || typeof asr !== "object") return preflight?.ok === true ? "ready" : "unloadable";
  if (asr.ok === true) return "ready";
  const error = String(asr.error || preflight.error || "");
  if (/LocalEntryNotFound|not available locally|SnapshotIncomplete|incomplete|HF_SNAPSHOT|FileNotFoundError/i.test(error)) return "missing";
  if (asr.local_only === true && asr.load_checked === false) return "missing";
  return "unloadable";
}

function otherDependenciesReady(preflight) {
  const modules = preflight?.modules;
  const modulesReady = Array.isArray(modules) && modules.length > 0 && modules.every((item) => item?.ok === true);
  const checkOk = (value) => !!value && typeof value === "object" && value.ok === true;
  return modulesReady && checkOk(preflight?.ffmpeg) && checkOk(preflight?.diarization_model) && checkOk(preflight?.artifact_root);
}

export function resolveWhisperPreflights(large, base, repoIds) {
  const largeState = asrState(large);
  const baseState = asrState(base);
  if (largeState === "ready" && large?.ok === true) return { status: "ready", model: "large-v3" };
  if (baseState === "ready" && base?.ok === true) return { status: "ready", model: "base" };

  // Whisper acquisition cannot repair missing worker packages, FFmpeg,
  // diarization, or artifact storage. Keep the complete preflight authoritative.
  if (!otherDependenciesReady(large) || !otherDependenciesReady(base)) {
    return { status: "error", model: null, category: "dependencies_missing",
      error: base?.error || large?.error || "Required processing dependencies are unavailable." };
  }
  if (largeState === "missing") return { status: "needs_download", model: null,
    downloadModel: "large-v3", repoId: large?.asr_model?.repo_id || repoIds.large,
    error: "Whisper large-v3 is not available locally. Download it to continue." };
  if (baseState === "missing") return { status: "needs_download", model: null,
    downloadModel: "base", repoId: base?.asr_model?.repo_id || repoIds.base,
    error: "Whisper base is not available locally. Download it to continue." };
  return { status: "error", model: null, category: "model_unusable",
    error: base?.asr_model?.error || large?.asr_model?.error || base?.error || large?.error || "No usable Whisper model for this hardware." };
}
