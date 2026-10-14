import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createSetupController } from '../src/server.js';

const wheelPath = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL;
const wheelSha256 = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256;
if (!wheelPath || !wheelSha256) {
  throw new Error('Set SPEECHCRAFT_MODEL_MANAGER_WHEEL and its trusted SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256 first.');
}

function directoryBytes(root) {
  let bytes = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, entry.name);
    if (entry.isSymbolicLink()) continue;
    if (entry.isDirectory()) bytes += directoryBytes(target);
    else if (entry.isFile()) bytes += fs.statSync(target).size;
  }
  return bytes;
}

async function installAndVerify(dataDirectory) {
  const app = await createSetupController({ dataDirectory, port: 0, mode: 'bootstrap',
    bootstrapOptions: { wheelPath, wheelSha256 } });
  try {
    const origin = `http://127.0.0.1:${app.port}`;
    const token = fs.readFileSync(app.bootstrapPath, 'utf8').trim();
    const sessionResponse = await fetch(`${origin}/setup-api/session`, { method: 'POST',
      headers: { origin, authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: '{}' });
    if (sessionResponse.status !== 201) throw new Error(`Could not establish local session (${sessionResponse.status}).`);
    const session = await sessionResponse.json();
    const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
    const response = await fetch(`${origin}/setup-api/start`, { method: 'POST', headers: {
      origin, cookie, 'x-speechcraft-csrf': session.csrf_token, 'content-type': 'application/json' }, body: '{}' });
    if (![200, 202].includes(response.status)) throw new Error(`Bootstrap start was rejected (${response.status}): ${await response.text()}`);
    const started = Date.now();
    while (Date.now() - started < 30 * 60 * 1000) {
      const statusResponse = await fetch(`${origin}/setup-api/status`, { headers: { cookie, origin } });
      const status = await statusResponse.json();
      if (['completed', 'failed', 'cancelled', 'interrupted'].includes(status.job.state)) {
        if (status.job.state !== 'completed') throw new Error(`Bootstrap failed in ${status.job.stage}: ${JSON.stringify(status.job.error)}`);
        if (!status.job.bootstrap_status?.bootstrap_ready || status.production_ready !== false) {
          throw new Error('Bootstrap did not report verified bootstrap components with production_ready=false.');
        }
        return { status, duration_ms: Date.now() - started };
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error('Bootstrap did not finish within 30 minutes. The isolated data directory was retained for recovery inspection.');
  } finally { await app.close(); }
}

const dataDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-real-bootstrap-'));
const platform = { node: process.version, os: process.platform, arch: process.arch };
const before = Date.now();
try {
  const first = await installAndVerify(dataDirectory);
  const afterFirstBytes = directoryBytes(dataDirectory);
  const second = await installAndVerify(dataDirectory);
  const afterSecondBytes = directoryBytes(dataDirectory);
  console.log(JSON.stringify({
    result: 'passed', platform, data_directory: dataDirectory,
    uv_version: '0.12.23', python_version: '3.12.15',
    first_duration_ms: first.duration_ms, second_duration_ms: second.duration_ms,
    first_store_bytes: afterFirstBytes, second_store_bytes: afterSecondBytes,
    installed_python: second.status.job.bootstrap_status.python_executable,
    model_manager_version: second.status.job.bootstrap_status.manager_version,
    production_ready: second.status.production_ready,
    elapsed_ms: Date.now() - before,
    note: 'The model-manager wheel is an explicit trusted local release input. No model weights are downloaded.',
  }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ result: 'failed', platform, data_directory: dataDirectory,
    error: error.message, retained_for_diagnostics: true }, null, 2));
  process.exitCode = 1;
}
