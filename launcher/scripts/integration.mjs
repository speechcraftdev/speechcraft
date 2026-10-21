import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { SetupController } from '../../setup-controller/src/controller.js';
import { RuntimeTasks } from '../../setup-controller/src/runtime-tasks.js';
import { StateStore } from '../../setup-controller/src/state-store.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const bin = path.join(root, 'bin/speechcraft');
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-launcher-e2e-'));
const origin = 'http://127.0.0.1:3002';
const launchedUrls = [];
let child;
let childOutput = '';

function startLauncher({ simulated = true } = {}) {
  // Exercise the prepared production server instead of compiling the entire
  // application on the first request through Next dev/Turbopack.
  const args = ['--data-dir', dataDir, '--no-browser', ...(simulated ? ['--simulated'] : [])];
  const launchEnv = { ...process.env };
  delete launchEnv.SPEECHCRAFT_MODEL_MANAGER_WHEEL;
  delete launchEnv.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256;
  const processHandle = spawn(bin, args, {
    cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: launchEnv,
  });
  processHandle.stdout.setEncoding('utf8').on('data', (value) => { childOutput += value; launchedUrls.push(...value.match(/http:\/\/127\.0\.0\.1:3002\/(?:setup|login)/g) || []); });
  processHandle.stderr.setEncoding('utf8').on('data', (value) => { childOutput += value; });
  return processHandle;
}

async function stopLauncher() {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const current = child;
  const exited = once(current, 'exit');
  current.kill('SIGTERM');
  let timer;
  try {
    await Promise.race([exited, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Launcher did not stop gracefully.')), 20_000); })]);
  } finally { clearTimeout(timer); }
  child = null;
}

async function get(url) {
  return fetch(url, { cache: 'no-store', redirect: 'manual', signal: AbortSignal.timeout(3000) });
}

async function acceptsConnection(host, port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port });
    const timer = setTimeout(() => { socket.destroy(); resolve(false); }, 1000);
    socket.once('connect', () => { clearTimeout(timer); socket.destroy(); resolve(true); });
    socket.once('error', () => { clearTimeout(timer); resolve(false); });
  });
}

class RuntimeCompletionFixture extends RuntimeTasks {
  constructor(options) {
    super(options);
    this.stages = ['verify_services'];
    this.service = { verify: async () => ({ port: 8010 }), stop: async () => {} };
  }
  initialBootstrapStatus() {
    const value = super.initialBootstrapStatus();
    value.profile = 'linux-x86_64';
    value.runtime_profile = { profile: 'linux-x86_64', workerProfile: 'linux-x86_64-cpu', accelerator: 'cpu' };
    value.uv_verified = true; value.python_verified = true; value.model_manager_verified = true;
    for (const name of ['managed_python', 'backend_environment', 'worker_environment']) {
      value.components[name] = { state: 'verified', verification: 'controlled_fixture', identity: `${name}:fixture` };
    }
    for (const name of ['ffmpeg', 'silero', 'whisper', 'pyannote', 'ecapa']) {
      value.components[name] = { state: 'runtime_ready', runtime_ready: true, verification: 'controlled_fixture', identity: `${name}:fixture` };
    }
    value.components.ffmpeg.redistribution_approved = false;
    return value;
  }
}

async function completeThroughControllerTransition() {
  const store = StateStore.open(dataDir);
  const tasks = new RuntimeCompletionFixture({ dataDirectory: store.directory, store });
  const controller = new SetupController(store, { tasks, mode: 'runtime' });
  try {
    assert.equal(store.snapshot().onboarding, null);
    controller.start();
    await controller.waitForIdle();
    assert.equal(store.snapshot().onboarding?.completed, true);
    assert.equal(store.snapshot().job.bootstrap_status.production_ready, false);
  } finally {
    await controller.shutdown();
    store.releaseOwnership();
  }
}

async function waitFor(url, predicate, timeout = 50_000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    if (child?.exitCode !== null && child?.exitCode !== undefined) throw new Error(`Launcher exited early: ${childOutput}`);
    try {
      const response = await get(url);
      if (await predicate(response)) return response;
    } catch (error) { lastError = error; }
    await delay(200);
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError?.message || 'no HTTP response'}\n${childOutput.slice(-3000)}`);
}

try {
  console.log('integration: starting launcher');
  child = startLauncher();
  const page = await waitFor(`${origin}/login`, (response) => response.ok);
  console.log('integration: frontend ready');
  assert.match(await page.text(), /Train better voice models\./);
  const healthResponse = await get(`${origin}/api/speechcraft-launcher/health`);
  const health = await healthResponse.json();
  assert.equal(healthResponse.status, 200);
  assert.equal(health.service, 'speechcraft-frontend');
  const cliStatus = spawnSync(bin, ['status', '--json', '--data-dir', dataDir], { cwd: root, encoding: 'utf8', env: { ...process.env } });
  console.log('integration: authenticated CLI status returned');
  assert.equal(cliStatus.status, 0, cliStatus.stderr);
  const statusValue = JSON.parse(cliStatus.stdout);
  assert.equal(statusValue.running, true);
  assert.equal(statusValue.frontend, 'verified');
  assert.equal(statusValue.controller, 'running');
  const networkAddress = Object.values(os.networkInterfaces()).flat().find((value) => value && value.family === 'IPv4' && !value.internal)?.address;
  if (networkAddress) {
    assert.equal(await acceptsConnection(networkAddress, 3002), false, 'the frontend is not exposed on a non-loopback interface');
    assert.equal(await acceptsConnection(networkAddress, 4317), false, 'the controller is not exposed on a non-loopback interface');
  }
  const bootstrap = fs.readFileSync(path.join(dataDir, 'bootstrap.token'), 'utf8').trim();
  const sessionResponse = await fetch(`${origin}/setup-api/session`, { method: 'POST', cache: 'no-store', headers: {
    host: '127.0.0.1:3002', origin, 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(sessionResponse.status, 201);
  const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
  const session = await sessionResponse.json();
  assert.equal(JSON.stringify(session).includes(bootstrap), false);
  const status = await fetch(`${origin}/setup-api/status`, { headers: { host: '127.0.0.1:3002', cookie } });
  const snapshot = await status.json();
  assert.equal(status.status, 200);
  assert.equal(snapshot.mode, 'simulated');
  assert.equal(snapshot.onboarding.completed, false);
  const start = await fetch(`${origin}/setup-api/start`, { method: 'POST', cache: 'no-store', headers: {
    host: '127.0.0.1:3002', origin, cookie, 'content-type': 'application/json',
    'x-speechcraft-csrf': session.csrf_token,
  }, body: '{}' });
  assert.equal(start.status, 202, 'a browser-origin authenticated setup action reaches the controller');
  console.log('integration: setup mutation accepted');
  const simulatedDeadline = Date.now() + 10_000;
  let simulatedStatus = snapshot;
  while (Date.now() < simulatedDeadline && simulatedStatus.job.state !== 'completed') {
    await delay(100);
    simulatedStatus = await (await fetch(`${origin}/setup-api/status`, { headers: { host: '127.0.0.1:3002', cookie } })).json();
  }
  assert.equal(simulatedStatus.job.state, 'completed');
  console.log('integration: simulated setup completed');
  assert.equal(simulatedStatus.onboarding.completed, false, 'completed simulation stays on the first-run setup route');
  const stream = await fetch(`${origin}/setup-api/events`, { headers: { host: '127.0.0.1:3002', cookie, accept: 'text/event-stream' } });
  assert.equal(stream.status, 200);
  const reader = stream.body.getReader();
  const first = await reader.read();
  assert.match(new TextDecoder().decode(first.value), /event: snapshot/);
  await reader.cancel();

  // A second invocation must identify and reuse the same controller and
  // frontend rather than launching duplicate services.
  const second = spawn(bin, ['--data-dir', dataDir, '--simulated', '--no-browser'], {
    cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env },
  });
  const [exitCode] = await once(second, 'exit');
  console.log('integration: duplicate launch reused existing instance');
  assert.equal(exitCode, 0);
  assert.equal((await get(`${origin}/login`)).status, 200);
  assert.equal(childOutput.includes(bootstrap), false);
  await stopLauncher();

  // Reach completion through SetupController + RuntimeTasks' authoritative
  // readiness evaluator. Only the prerequisite evidence is a controlled
  // fixture; production code performs the state transition and persistence.
  await completeThroughControllerTransition();
  console.log('integration: onboarding completion persisted by controller');
  child = startLauncher({ simulated: false });
  const normalEntry = await waitFor(`${origin}/login`, (response) => response.ok);
  assert.equal(normalEntry.status, 200);
  const urlDeadline = Date.now() + 25_000;
  while (Date.now() < urlDeadline && !launchedUrls.some((value) => value.endsWith('/login'))) await delay(100);
  assert.ok(launchedUrls.some((value) => value.endsWith('/login')),
    `a valid durable completion record routes to SpeechCraft after restart (URLs: ${launchedUrls.join(', ')}; output: ${childOutput.slice(-1000)})`);
  assert.equal((await get(`${origin}/login`)).status, 200, 'setup remains directly accessible after completion');
  const renewedSessionResponse = await fetch(`${origin}/setup-api/session`, { method: 'POST', headers: {
    host: '127.0.0.1:3002', origin, 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(renewedSessionResponse.status, 201, 'session can be re-established after controller restart');
  const renewedCookie = renewedSessionResponse.headers.get('set-cookie').split(';', 1)[0];
  const persistedStatus = await (await fetch(`${origin}/setup-api/status`, { headers: {
    host: '127.0.0.1:3002', cookie: renewedCookie,
  } })).json();
  assert.equal(persistedStatus.job.state, 'completed', 'service restoration does not create a new setup job');
  assert.equal(persistedStatus.onboarding.completed, true, 'onboarding remains sticky after controller restart');
  const cliStop = spawnSync(bin, ['stop', '--data-dir', dataDir], { cwd: root, encoding: 'utf8', env: { ...process.env } });
  assert.equal(cliStop.status, 0, cliStop.stderr || cliStop.stdout);
  assert.match(cliStop.stdout, /SpeechCraft stopped/);
  if (child.exitCode === null && child.signalCode === null) await once(child, 'exit');
  console.log('Launcher HTTP integration passed: loopback UI, session bridge, authenticated status/SSE, duplicate-launch reuse, controller-driven onboarding completion, wheel-free repeat launch, sticky first-run routing, and authenticated stop.');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  let stopped = !child || child.exitCode !== null || child.signalCode !== null;
  if (child && child.exitCode === null && child.signalCode === null) {
    const exited = once(child, 'exit');
    child.kill('SIGTERM');
    let timer;
    try {
      await Promise.race([exited, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Launcher did not stop gracefully.')), 20_000); })]);
      stopped = true;
    } catch (error) { console.error(error.message); process.exitCode = 1; }
    finally { clearTimeout(timer); }
  }
  if (stopped) fs.rmSync(dataDir, { recursive: true, force: true });
  else console.error(`Preserving isolated test data because process shutdown is uncertain: ${dataDir}`);
}
