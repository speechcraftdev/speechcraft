import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveWhisperPreflights } from "./model-resolution.mjs";

const repos = { large: "Systran/faster-whisper-large-v3", base: "Systran/faster-whisper-base" };
const dependencies = () => ({ modules: [{ ok: true }, { ok: true }], ffmpeg: { ok: true },
  diarization_model: { ok: true }, artifact_root: { ok: true } });
const ready = (model) => ({ ok: true, ...dependencies(), asr_model: { ok: true, model } });
const missing = (repo_id) => ({ ok: false, ...dependencies(), asr_model: { ok: false, local_only: true,
  load_checked: false, error: "Model is not available locally", repo_id } });

test("requires the full worker preflight and prefers a usable large-v3 model", () => {
  assert.deepEqual(resolveWhisperPreflights(ready("large-v3"), ready("base"), repos), { status: "ready", model: "large-v3" });
  const broken = { ...ready("large-v3"), ok: false, ffmpeg: { ok: false, error: "ffmpeg missing" } };
  assert.deepEqual(resolveWhisperPreflights(broken, broken, repos), {
    status: "error", model: null, category: "dependencies_missing", error: "Required processing dependencies are unavailable.",
  });
});

test("falls back from an unloadable large model to base and only downloads after full preflight", () => {
  const largeBad = { ok: false, ...dependencies(), asr_model: { ok: false, local_only: true,
    load_checked: true, error: "model does not fit available memory" } };
  assert.deepEqual(resolveWhisperPreflights(largeBad, ready("base"), repos), { status: "ready", model: "base" });
  assert.deepEqual(resolveWhisperPreflights(missing(repos.large), missing(repos.base), repos), {
    status: "needs_download", model: null, downloadModel: "large-v3", repoId: repos.large,
    error: "Whisper large-v3 is not available locally. Download it to continue.",
  });
});

test("does not classify an ASR initialization failure as a cache miss", () => {
  const failedLoad = { ok: false, ...dependencies(), asr_model: { ok: false, local_only: true,
    load_checked: true, error: "RuntimeError: unsupported compute type" } };
  assert.equal(resolveWhisperPreflights(failedLoad, failedLoad, repos).category, "model_unusable");
  const incompleteReport = { ok: false, asr_model: { ok: false } };
  assert.equal(resolveWhisperPreflights(incompleteReport, incompleteReport, repos).category, "dependencies_missing");
});
