import assert from "node:assert/strict";
import { test } from "node:test";
import { downloadPercent, latestProgressForJob, setupActionFor, setupFailureCodeFor, setupFailureFor } from "./model-onboarding.mjs";

const base = { job: { state: "idle", error: null }, technical_processing_ready: false, components: { pyannote: { state: "pending" } } };

test("managed setup action requires verification before consent, and never starts from an idle status", () => {
  assert.equal(setupActionFor(base), "verify");
  assert.equal(setupActionFor({ ...base, job: { state: "waiting_for_user" } }), "verify");
  assert.equal(setupActionFor({ ...base, components: { pyannote: { state: "runtime_ready" } } }), "download");
});

test("active setup reconnects while completed setup finalizes only after readiness", () => {
  assert.equal(setupActionFor({ ...base, job: { state: "running" } }), "watch");
  assert.equal(setupActionFor({ ...base, job: { state: "completed" }, technical_processing_ready: false }), "recover");
  assert.equal(setupActionFor({ ...base, job: { state: "completed" }, technical_processing_ready: true }), "finalize");
});

test("authorization failures return to verification and interrupted jobs require explicit recovery", () => {
  assert.equal(setupActionFor({ ...base, job: { state: "failed", error: { code: "HF_TOKEN_INVALID" } } }), "verify");
  for (const state of ["failed", "cancelled", "interrupted"]) {
    assert.equal(setupActionFor({ ...base, job: { state } }), "recover");
  }
});

test("setup errors are categorized with an actionable next step", () => {
  assert.equal(setupFailureFor("HF_TOKEN_INVALID").category, "hugging_face_token");
  assert.equal(setupFailureFor("HF_MODEL_ACCESS_REQUIRED").category, "model_conditions");
  assert.equal(setupFailureFor("HF_NETWORK_ERROR").category, "network");
  assert.equal(setupFailureFor("BACKEND_SERVICE_UNAVAILABLE").action, "retry");
  assert.equal(setupFailureFor("WORKER_ENVIRONMENT_INVALID").category, "python_runtime");
  assert.equal(setupFailureFor("MODEL_SNAPSHOT_INTEGRITY_FAILED").category, "model_verification");
  assert.equal(setupFailureFor("MODEL_INITIALIZATION_FAILED").category, "model_initialization");
  assert.equal(setupFailureCodeFor({ job: { state: "failed", stage: "install_models", error: { code: "SETUP_TASK_FAILED" } } }), "MODEL_DOWNLOAD_FAILED");
  assert.equal(setupFailureCodeFor({ job: { state: "completed", stage: "verify_services" }, technical_processing_ready: false }), "PROCESSING_PREREQUISITES_MISSING");
});

test("download progress uses byte totals and hides percentages for non-transfer or indeterminate steps", () => {
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "bytes", transferred_bytes: 5_000,
    total_bytes: 20_000, percent: 25 }), 25);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "bytes", transferred_bytes: 3_000,
    total_bytes: 4_000, percent: 75 }), 75);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "bytes", transferred_bytes: 20,
    total_bytes: 10, percent: 100 }), null, "invalid counters are not trusted even if a percentage is supplied");
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "files", completed_files: 2, total_files: 8 }), null);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "files", completed_files: 8, total_files: 8 }), null);
  assert.equal(downloadPercent({ operation: "installing", progress_mode: "bytes", transferred_bytes: 3,
    total_bytes: 4, percent: 75 }), null);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "indeterminate", transferred_bytes: 3,
    total_bytes: 4, percent: 75 }), null);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "bytes", transferred_bytes: 45,
    total_bytes: 100, percent: 45 }), 45);
  assert.equal(downloadPercent({ operation: "downloading", progress_mode: "bytes", transferred_bytes: 45,
    total_bytes: 100, percent: Number.NaN }), null);
});

test("progress selection rejects stale job attempts and prefers the newest event revision", () => {
  const current = { job_id: "job-1", attempt_id: "attempt-1", event_revision: 12, artifact_name: "model-B" };
  const result = latestProgressForJob("job-1", "attempt-1", [
    { job_id: "job-1", attempt_id: "attempt-1", event_revision: 10, artifact_name: "model-A" },
    { job_id: "job-1", attempt_id: "attempt-0", event_revision: 99, artifact_name: "old attempt" },
    current,
  ]);
  assert.equal(result, current);
});
