#!/usr/bin/env node
// End-to-end test of a generated Linux archive and its installed shell command.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { ownedProcessIdentity } from '../../setup-controller/src/bootstrap.js';

const args = process.argv.slice(2);
const get = (name) => { const i = args.indexOf(name); return i < 0 ? null : args[i + 1]; };
const archive = path.resolve(get('--archive') || '');
const installer = path.resolve(get('--installer') || '');
const sourceCheckout = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
if (!get('--archive') || !get('--installer')) throw new Error('Usage: installed-linux-smoke.mjs --archive ARCHIVE --installer INSTALLER');
const base = fs.mkdtempSync(path.join(os.tmpdir(), 'SpeechCraft Installed Smoke '));
const home = path.join(base, 'User Home With Spaces'); fs.mkdirSync(home, { mode: 0o700 });
const data = path.join(base, 'Persistent Data With Spaces');
const tools = path.join(base, 'tools'); fs.mkdirSync(tools);
for (const name of ['dirname', 'readlink']) fs.symlinkSync(`/usr/bin/${name}`, path.join(tools, name));
const env = { ...process.env, HOME: home, PATH: tools, SPEECHCRAFT_SETUP_DATA_DIR: undefined };
const run = (command, commandArgs, options = {}) => {
  const result = spawnSync(command, commandArgs, { env, cwd: base, encoding: 'utf8', shell: false, ...options });
  if (result.error || result.status !== 0) throw new Error(`${command} exited ${result.status}: ${result.stderr || result.error?.message}`);
  return result.stdout;
};
const waitFor = async (url, test = (response) => response.ok) => {
  const deadline = Date.now() + 30_000; let lastError;
  while (Date.now() < deadline) {
    try { const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(800) }); if (await test(response)) return response; }
    catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError?.message || 'no response'}`);
};
const start = (dataDirectory, simulated = true) => spawn(path.join(home, '.local/bin/speechcraft'), [...(simulated ? ['--simulated'] : []), '--no-browser', '--data-dir', dataDirectory],
  { cwd: base, env, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
let app;
let fakeBackend;
let output = [];
try {
  run('/usr/bin/python3', [installer, archive]);
  run('/usr/bin/python3', [installer, archive]); // Repeat installation must be idempotent.
  const command = path.join(home, '.local/bin/speechcraft');
  assert.match(fs.readFileSync(path.join(home, '.profile'), 'utf8'), /SpeechCraft user command PATH/);
  const loginShell = spawnSync('/bin/bash', ['-lc', 'command -v speechcraft'], { env, cwd: base, encoding: 'utf8', shell: false });
  assert.equal(loginShell.status, 0, loginShell.stderr);
  assert.equal(loginShell.stdout.trim(), command, 'fresh login shell resolves the installed command through the managed PATH entry');
  assert.match(run(command, ['--help']), /Usage: speechcraft/);
  assert.doesNotThrow(() => fs.accessSync(command, fs.constants.X_OK));
  fakeBackend = http.createServer(async (request, response) => {
    if (request.url === '/health') { response.setHeader('content-type', 'application/json'); response.end('{"status":"ok"}'); return; }
    if (request.url === '/audio') {
      response.statusCode = request.headers.range ? 206 : 200;
      if (request.headers.range) response.setHeader('content-range', 'bytes 2-5/10');
      response.setHeader('accept-ranges', 'bytes'); response.end('2345'); return;
    }
    if (request.url === '/upload') {
      let bytes = 0; for await (const chunk of request) bytes += chunk.length;
      response.setHeader('content-type', 'application/json'); response.end(JSON.stringify({ bytes })); return;
    }
    response.statusCode = 404; response.end();
  });
  await new Promise((resolve, reject) => { fakeBackend.once('error', reject); fakeBackend.listen(8010, '127.0.0.1', resolve); });
  const runtimeData = path.join(base, 'Runtime Mode Persistent Data');
  app = start(runtimeData, false);
  output = [];
  app.stdout.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  app.stderr.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  await waitFor('http://127.0.0.1:3002/setup');
  const urlDeadline = Date.now() + 2_000;
  while (!output.join('').includes('SpeechCraft is available at') && Date.now() < urlDeadline) await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(output.join(''), /SpeechCraft is available at http:\/\/127\.0\.0\.1:3002\/setup/);
  const runtimeSession = await fetch('http://127.0.0.1:3002/setup-api/session', { method: 'POST', headers: {
    origin: 'http://127.0.0.1:3002', 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(runtimeSession.status, 201);
  const runtimeCookie = runtimeSession.headers.get('set-cookie').split(';', 1)[0];
  const runtimeModels = await (await fetch('http://127.0.0.1:3002/setup-api/models', { headers: { cookie: runtimeCookie } })).json();
  assert.deepEqual(runtimeModels.models.map((model) => model.state), ['pending', 'pending', 'pending', 'pending']);
  const runtimeExit = once(app, 'exit');
  run(command, ['stop', '--data-dir', runtimeData]);
  if (app.exitCode === null && app.signalCode === null) await runtimeExit;
  assert.doesNotMatch(output.join(''), /exited unexpectedly/, 'authenticated stop is not surfaced as an application crash');
  app = null;
  app = start(data);
  output = [];
  app.stdout.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  app.stderr.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  await waitFor('http://127.0.0.1:3002/setup');
  for (const route of ['/login', '/lab']) assert.equal((await fetch(`http://127.0.0.1:3002${route}`)).status, 200);
  const sessionResponse = await fetch('http://127.0.0.1:3002/setup-api/session', { method: 'POST', headers: {
    origin: 'http://127.0.0.1:3002', 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(sessionResponse.status, 201);
  const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
  const { csrf_token: csrf } = await sessionResponse.json();
  assert.ok(cookie && csrf);
  const statusResponse = await fetch('http://127.0.0.1:3002/setup-api/status', { headers: { cookie } });
  assert.equal(statusResponse.status, 200);
  const status = await statusResponse.json(); assert.equal(status.onboarding.completed, false);
  const cliStatus = JSON.parse(run(command, ['status', '--json', '--data-dir', data]));
  assert.equal(cliStatus.frontend, 'verified'); assert.equal(cliStatus.controller, 'running');
  const eventResponse = await fetch('http://127.0.0.1:3002/setup-api/events', { headers: { cookie, accept: 'text/event-stream' }, signal: AbortSignal.timeout(2_000) });
  assert.equal(eventResponse.status, 200);
  const eventReader = eventResponse.body.getReader();
  const firstEvent = await eventReader.read();
  assert.match(new TextDecoder().decode(firstEvent.value), /snapshot|event:/i);
  await eventReader.cancel();
  const mutation = await fetch('http://127.0.0.1:3002/setup-api/start', { method: 'POST', headers: {
    origin: 'http://127.0.0.1:3002', cookie, 'x-speechcraft-csrf': csrf, 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(mutation.status, 202);
  let setupState = await (await fetch('http://127.0.0.1:3002/setup-api/status', { headers: { cookie } })).json();
  const setupDeadline = Date.now() + 10_000;
  while (setupState.job?.state !== 'completed' && Date.now() < setupDeadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    setupState = await (await fetch('http://127.0.0.1:3002/setup-api/status', { headers: { cookie } })).json();
  }
  assert.equal(setupState.job.state, 'completed');
  const rejected = await fetch('http://127.0.0.1:3002/setup-api/session', { method: 'POST', headers: {
    origin: 'http://attacker.invalid', 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(rejected.status, 403);
  const pageHtml = await (await fetch('http://127.0.0.1:3002/setup')).text();
  const assetPath = pageHtml.match(/\/_next\/static\/[^"']+\.(?:css|js)/)?.[0];
  assert.ok(assetPath, 'standalone page references a packaged static asset');
  assert.equal((await fetch(`http://127.0.0.1:3002${assetPath}`)).status, 200);
  assert.equal((await fetch('http://127.0.0.1:3002/sc-api/health')).status, 200);
  const rangedAudio = await fetch('http://127.0.0.1:3002/sc-api/audio', { headers: { range: 'bytes=2-5' } });
  assert.equal(rangedAudio.status, 206); assert.equal(rangedAudio.headers.get('content-range'), 'bytes 2-5/10');
  assert.equal(await rangedAudio.text(), '2345');
  const uploadBytes = Buffer.alloc(11 * 1024 * 1024, 0x61);
  const uploadResponse = await fetch('http://127.0.0.1:3002/sc-api/upload', { method: 'POST', headers: { 'content-type': 'application/octet-stream' }, body: uploadBytes });
  assert.equal(uploadResponse.status, 200);
  assert.equal((await uploadResponse.json()).bytes, uploadBytes.length);
  const noNodeVersion = run(command, ['--help']); assert.match(noNodeVersion, /Usage/);
  const releaseRoot = fs.realpathSync(path.join(home, '.local/share/speechcraft/app/current'));
  const packagedBackend = path.join(releaseRoot, 'backend');
  const packagedWorker = path.join(releaseRoot, 'workers/dataset');
  assert.ok(fs.statSync(path.join(packagedBackend, 'app/dataset_worker_client.py')).isFile());
  assert.ok(fs.statSync(path.join(packagedBackend, 'app/dataset_runs.py')).isFile());
  assert.ok(fs.statSync(path.join(packagedWorker, 'scripts/preflight.py')).isFile());
  assert.ok(fs.statSync(path.join(packagedWorker, 'scripts/runtime_probe.py')).isFile());
  const preflightCall = spawnSync('/usr/bin/python3', ['-c', [
    'import json',
    'from app.dataset_worker_client import run_dataset_worker_preflight',
    'print(json.dumps(run_dataset_worker_preflight(asr_model="large-v3")))',
  ].join('; ')], { cwd: base, env: { ...env, PYTHONPATH: packagedBackend,
    SPEECHCRAFT_DATASET_WORKER_ROOT: packagedWorker,
    SPEECHCRAFT_DATASET_WORKER_PYTHON: '/usr/bin/python3', SPEECHCRAFT_MANAGED_RUNTIME: '1' },
  encoding: 'utf8', shell: false, timeout: 30_000 });
  assert.equal(preflightCall.status, 0, preflightCall.stderr);
  const packagedPreflight = JSON.parse(preflightCall.stdout);
  assert.equal(packagedPreflight.worker_root, packagedWorker);
  assert.equal(packagedPreflight.command[1], path.join(packagedWorker, 'scripts/preflight.py'));
  assert.equal(packagedPreflight.ok, false, 'fixture environment has no installed production ML dependencies');
  assert.match(JSON.stringify(packagedPreflight), /ModuleNotFoundError|not configured|not found/i,
    'the production entrypoint reports actionable missing runtime dependencies');
  assert.equal(JSON.stringify(packagedPreflight).includes(sourceCheckout), false,
    'worker invocation is resolved only from the installed package, never the source checkout');
  const releaseNode = path.join(releaseRoot, 'runtime/node');
  const records = JSON.parse(fs.readFileSync(path.join(data, 'launcher.processes.json'), 'utf8')).records;
  assert.equal(records.length, 2);
  for (const record of records) assert.ok(record.identity.startsWith(`linux:${fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim()}:`));
  assert.equal(fs.realpathSync(`/proc/${records[0].pid}/exe`), releaseNode, 'controller executes bundled Node');
  const frontendExe = fs.realpathSync(`/proc/${records[1].pid}/exe`);
  assert.equal(frontendExe, releaseNode, 'frontend executes bundled Node');
  const cleanExit = once(app, 'exit');
  run(command, ['stop', '--data-dir', data]);
  if (app.exitCode === null && app.signalCode === null) await cleanExit;
  app = null;
  assert.equal((await fetch('http://127.0.0.1:3002/setup').catch(() => null)), null);

  // Create onboarding completion through the packaged controller's real
  // readiness evaluator, using explicit controlled fixture evidence.
  const { pathToFileURL } = await import('node:url');
  const [{ SetupController }, { RuntimeTasks }, { StateStore }] = await Promise.all([
    import(pathToFileURL(path.join(releaseRoot, 'setup-controller/src/controller.js'))),
    import(pathToFileURL(path.join(releaseRoot, 'setup-controller/src/runtime-tasks.js'))),
    import(pathToFileURL(path.join(releaseRoot, 'setup-controller/src/state-store.js'))),
  ]);
  class VerifiedRuntimeFixture extends RuntimeTasks {
    constructor(options) { super(options); this.stages = ['verify_services']; this.service = { verify: async () => ({ port: 8010 }), stop: async () => {} }; }
    initialBootstrapStatus() {
      const value = super.initialBootstrapStatus();
      value.profile = 'linux-x86_64'; value.runtime_profile = { profile: 'linux-x86_64', workerProfile: 'linux-x86_64-cpu', accelerator: 'cpu' };
      value.uv_verified = true; value.python_verified = true; value.model_manager_verified = true;
      for (const name of ['managed_python', 'backend_environment', 'worker_environment']) value.components[name] = { state: 'verified', verification: 'fixture', identity: `${name}:fixture` };
      for (const name of ['ffmpeg', 'silero', 'whisper', 'pyannote', 'ecapa']) value.components[name] = { state: 'runtime_ready', runtime_ready: true, verification: 'fixture', identity: `${name}:fixture` };
      value.components.ffmpeg.redistribution_approved = false;
      return value;
    }
  }
  const store = StateStore.open(data);
  const fixtureController = new SetupController(store, { tasks: new VerifiedRuntimeFixture({ dataDirectory: store.directory, store }), mode: 'runtime' });
  try {
    assert.equal(store.snapshot().onboarding, null);
    fixtureController.start(); await fixtureController.waitForIdle();
    assert.equal(store.snapshot().onboarding?.completed, true);
    assert.equal(store.snapshot().job.bootstrap_status.production_ready, false);
  } finally { await fixtureController.shutdown(); store.releaseOwnership(); }

  app = start(data, false);
  app.stdout.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  app.stderr.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  await waitFor('http://127.0.0.1:3002/login');
  const repeatStatus = await fetch('http://127.0.0.1:3002/setup-api/session', { method: 'POST', headers: {
    origin: 'http://127.0.0.1:3002', 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(repeatStatus.status, 201);
  const repeatCookie = repeatStatus.headers.get('set-cookie').split(';', 1)[0];
  const repeatSetupStatus = await fetch('http://127.0.0.1:3002/setup-api/status', { headers: { cookie: repeatCookie } });
  assert.equal((await repeatSetupStatus.json()).onboarding.completed, true);
  const repeatExit = once(app, 'exit');
  run(command, ['stop', '--data-dir', data]);
  if (app.exitCode === null && app.signalCode === null) await repeatExit;
  app = null;

  // Reproduce partial recovery: the first launcher dies, its controller
  // survives, its frontend disappears, and a new launcher reuses the controller
  // while creating a replacement frontend.
  const crashData = `${data}-front-crash`;
  app = start(crashData);
  await waitFor('http://127.0.0.1:3002/setup');
  const crashRecords = JSON.parse(fs.readFileSync(path.join(crashData, 'launcher.processes.json'), 'utf8')).records;
  const controllerRecord = crashRecords.find((record) => record.label === 'setup controller');
  const frontendRecord = crashRecords.find((record) => record.label === 'Next.js frontend');
  assert.equal(ownedProcessIdentity(controllerRecord.pid, controllerRecord.nonce), controllerRecord.identity);
  assert.equal(ownedProcessIdentity(frontendRecord.pid, frontendRecord.nonce), frontendRecord.identity);
  const crashedLauncher = once(app, 'exit');
  app.kill('SIGKILL');
  await crashedLauncher; app = null;
  process.kill(-frontendRecord.pid, 'SIGKILL');
  await new Promise((resolve) => setTimeout(resolve, 100));
  app = start(crashData);
  output = [];
  app.stdout.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  app.stderr.setEncoding('utf8').on('data', (chunk) => output.push(chunk));
  await waitFor('http://127.0.0.1:3002/setup');
  const recoveredRecords = JSON.parse(fs.readFileSync(path.join(crashData, 'launcher.processes.json'), 'utf8')).records;
  const recoveredController = recoveredRecords.find((record) => record.label === 'setup controller');
  const recoveredFrontend = recoveredRecords.find((record) => record.label === 'Next.js frontend');
  assert.equal(recoveredController.pid, controllerRecord.pid, 'surviving controller ownership is retained');
  assert.equal(recoveredController.identity, controllerRecord.identity);
  assert.notEqual(recoveredFrontend.pid, frontendRecord.pid, 'replacement frontend is recorded');
  assert.equal(ownedProcessIdentity(recoveredController.pid, recoveredController.nonce), recoveredController.identity);
  assert.equal(ownedProcessIdentity(recoveredFrontend.pid, recoveredFrontend.nonce), recoveredFrontend.identity);
  const recoveredStatus = JSON.parse(run(command, ['status', '--json', '--data-dir', crashData]));
  assert.equal(recoveredStatus.controller, 'running');
  assert.equal(recoveredStatus.frontend, 'verified');
  const recoveredExit = once(app, 'exit');
  run(command, ['stop', '--data-dir', crashData]);
  if (app.exitCode === null && app.signalCode === null) await recoveredExit;
  assert.doesNotMatch(output.join(''), /exited unexpectedly/, 'recovered controller stops without a false crash report');
  app = null;
  assert.equal((await fetch('http://127.0.0.1:4317/setup-api/health').catch(() => null)), null,
    'stop after partial recovery also terminates the surviving controller');

  // A dead controller must not prevent stop from reconciling its independently
  // owned frontend process.
  const controllerCrashData = `${data}-controller-crash`;
  app = start(controllerCrashData);
  await waitFor('http://127.0.0.1:3002/setup');
  const controllerCrashRecords = JSON.parse(fs.readFileSync(path.join(controllerCrashData, 'launcher.processes.json'), 'utf8')).records;
  const deadController = controllerCrashRecords.find((record) => record.label === 'setup controller');
  const liveFrontend = controllerCrashRecords.find((record) => record.label === 'Next.js frontend');
  assert.equal(ownedProcessIdentity(deadController.pid, deadController.nonce), deadController.identity);
  assert.equal(ownedProcessIdentity(liveFrontend.pid, liveFrontend.nonce), liveFrontend.identity);
  process.kill(-deadController.pid, 'SIGKILL');
  const controllerDeadline = Date.now() + 5_000;
  while (Date.now() < controllerDeadline) {
    try { await fetch('http://127.0.0.1:4317/setup-api/health', { signal: AbortSignal.timeout(200) }); }
    catch { break; }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal((await fetch('http://127.0.0.1:3002/api/speechcraft-launcher/health')).status, 200,
    'frontend stays available after controller crash');
  run(command, ['stop', '--data-dir', controllerCrashData]);
  await assert.rejects(fetch('http://127.0.0.1:3002/api/speechcraft-launcher/health', { signal: AbortSignal.timeout(300) }));
  const controllerCrashLauncherExit = once(app, 'exit');
  app.kill('SIGTERM');
  await controllerCrashLauncherExit;
  app = null;
  console.log('Installed Linux package smoke passed: idempotent install, bundled Node, external cwd, setup/login/lab and static assets, session, CSRF mutation, SSE, Origin rejection, backend proxy, audio range and 11 MiB upload, packaged worker preflight resolution, first-run route, completion through the packaged controller evaluator, repeat /login route, normal stop, recovery after launcher crash, and stop with either controller or frontend already crashed.');
} catch (error) {
  if (app && app.exitCode === null) app.kill('SIGTERM');
  throw new Error(`${error.message}\n${output?.join('').slice(-3000) || ''}`, { cause: error });
} finally {
  if (fakeBackend?.listening) await new Promise((resolve) => fakeBackend.close(resolve));
  if (fs.existsSync(base)) {
    for (const root of [base, ...fs.readdirSync(base).map((name) => path.join(base, name))]) {
      if (!fs.existsSync(root)) continue;
      const entries = [root];
      const visit = (directory) => { for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
        const filename = path.join(directory, item.name); entries.push(filename); if (item.isDirectory()) visit(filename);
      } };
      if (fs.statSync(root).isDirectory()) visit(root);
      for (const entry of entries.reverse()) {
        try { fs.chmodSync(entry, fs.statSync(entry).isDirectory() ? 0o700 : 0o600); } catch {}
      }
    }
    fs.rmSync(base, { recursive: true, force: true });
  }
}
