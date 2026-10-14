import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createSetupController } from '../src/server.js';

const wheelPath = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL;
const wheelSha256 = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256;
const applicationRoot = process.env.SPEECHCRAFT_APPLICATION_ROOT;
if (!wheelPath || !wheelSha256 || !applicationRoot) {
  throw new Error('Set the trusted model-manager wheel path/hash and application root before running the opt-in runtime smoke.');
}

function bytes(root) {
  if (!fs.existsSync(root)) return 0;
  let total = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) total += bytes(target);
    else if (entry.isFile()) total += fs.statSync(target).size;
  }
  return total;
}

const dataDirectory = process.env.SPEECHCRAFT_RUNTIME_SMOKE_DIR
  ? path.resolve(process.env.SPEECHCRAFT_RUNTIME_SMOKE_DIR)
  : fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-smoke-'));
fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
const platform = { node: process.version, os: process.platform, arch: process.arch };

async function runPlan() {
  const app = await createSetupController({ dataDirectory, mode: 'runtime', port: 0,
    bootstrapOptions: { wheelPath, wheelSha256, applicationRoot } });
  try {
    const origin = `http://127.0.0.1:${app.port}`;
    const token = fs.readFileSync(app.bootstrapPath, 'utf8').trim();
    const sessionResponse = await fetch(`${origin}/setup-api/session`, { method: 'POST', headers: {
      origin, authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: '{}' });
    if (sessionResponse.status !== 201) throw new Error(`Session establishment failed (${sessionResponse.status}).`);
    const session = await sessionResponse.json();
    const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
    const response = await fetch(`${origin}/setup-api/start`, { method: 'POST', headers: {
      origin, cookie, 'x-speechcraft-csrf': session.csrf_token, 'content-type': 'application/json' }, body: '{}' });
    if (![200, 202].includes(response.status)) throw new Error(`Start rejected (${response.status}): ${await response.text()}`);
    const started = Date.now();
    while (Date.now() - started < 8 * 60 * 60 * 1000) {
      const statusResponse = await fetch(`${origin}/setup-api/status`, { headers: { cookie, origin } });
      const status = await statusResponse.json();
      if (['completed', 'failed', 'cancelled', 'interrupted'].includes(status.job.state)) {
        if (status.job.state !== 'completed') throw new Error(`Runtime plan failed at ${status.job.stage}: ${JSON.stringify(status.job.error)}`);
        return { status, duration_ms: Date.now() - started };
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
    throw new Error('Runtime plan exceeded eight hours; preserve this isolated directory for recovery diagnostics.');
  } finally { await app.close(); }
}

try {
  const first = await runPlan();
  const firstBytes = { models: bytes(path.join(dataDirectory, 'models')),
    hf_cache: bytes(path.join(dataDirectory, 'cache', 'bootstrap', 'huggingface')) };
  const second = await runPlan();
  const secondBytes = { models: bytes(path.join(dataDirectory, 'models')),
    hf_cache: bytes(path.join(dataDirectory, 'cache', 'bootstrap', 'huggingface')) };
  const components = second.status.components || {};
  if (components.silero?.state !== 'files_verified' || components.whisper?.state !== 'files_verified'
      || second.status.production_ready !== false) throw new Error('Model files were not verified or production readiness was incorrectly claimed.');
  console.log(JSON.stringify({ result: 'passed', platform, profile: second.status.job.bootstrap_status.runtime_profile,
    data_directory: dataDirectory, model_root: path.join(dataDirectory, 'models'),
    hf_cache: path.join(dataDirectory, 'cache', 'bootstrap', 'huggingface'),
    first_duration_ms: first.duration_ms, second_duration_ms: second.duration_ms,
    first_bytes: firstBytes, second_bytes: secondBytes,
    silero: components.silero?.report, whisper: components.whisper?.report,
    blockers: Object.fromEntries(Object.entries(components).filter(([, value]) => value.state === 'blocked')),
    production_ready: second.status.production_ready,
    note: 'Large network download and cache behavior measured in an isolated SpeechCraft store.' }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ result: 'failed', platform, data_directory: dataDirectory,
    error: error.message, retained_for_recovery: true }, null, 2));
  process.exitCode = 1;
}
