import assert from "node:assert/strict";
import { test } from "node:test";
import { downloadPercent, setupActionFor, setupFailureCodeFor, setupFailureFor } from "./model-onboarding.mjs";

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
  assert.equal(downloadPercent({ phase: "downloading", current: 5_000, total: 20_000 }), 25);
  assert.equal(downloadPercent({ phase: "downloading", downloaded_bytes: 3_000, total_bytes: 4_000 }), 75);
  assert.equal(downloadPercent({ phase: "downloading", current: 20, total: 10 }), 100);
  assert.equal(downloadPercent({ phase: "downloading", files_completed: 2, files_total: 8, transfer_indeterminate: true }), 25);
  assert.equal(downloadPercent({ phase: "downloading", files_completed: 8, files_total: 8 }), 100);
  assert.equal(downloadPercent({ phase: "installing", current: 3, total: 4, percent: 75 }), null);
  assert.equal(downloadPercent({ phase: "downloading", current: 3, total: 4, transfer_indeterminate: true }), null);
  assert.equal(downloadPercent({ phase: "downloading", percent: 45 }), 45);
  assert.equal(downloadPercent({ phase: "downloading", percent: Number.NaN }), null);
});
