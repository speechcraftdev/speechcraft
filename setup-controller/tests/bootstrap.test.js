import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import test from 'node:test';
import { BootstrapError, BootstrapTasks, extractUvArchive, reconcileOwnedProcess, runOwnedProcess, stageWorkerBuildSources } from '../src/bootstrap.js';
import { SetupController } from '../src/controller.js';
import { StateStore } from '../src/state-store.js';

function hash(bytes) { return createHash('sha256').update(bytes).digest('hex'); }

function tarEntry(name, bytes = Buffer.alloc(0), type = '0') {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, 'utf8');
  header.write('0000700\0', 100, 8, 'ascii');
  header.write('0000000\0', 108, 8, 'ascii');
  header.write('0000000\0', 116, 8, 'ascii');
  header.write(`${bytes.length.toString(8).padStart(11, '0')}\0`, 124, 12, 'ascii');
  header.write('00000000000\0', 136, 12, 'ascii');
  header.fill(32, 148, 156);
  header.write(type, 156, 1, 'ascii');
  header.write('ustar\0', 257, 6, 'ascii');
  header.write('00', 263, 2, 'ascii');
  const checksum = header.reduce((sum, value) => sum + value, 0);
  header.write(`${checksum.toString(8).padStart(6, '0')}\0 `, 148, 8, 'ascii');
  const padding = Buffer.alloc((512 - (bytes.length % 512)) % 512);
  return Buffer.concat([header, bytes, padding]);
}

function archiveFor(files) {
  const entries = [tarEntry('fixture-uv/', Buffer.alloc(0), '5'), ...Object.entries(files).map(([name, value]) =>
    tarEntry(`fixture-uv/${name}`, Buffer.from(value), '0')), Buffer.alloc(1024)];
  return gzipSync(Buffer.concat(entries));
}

function setTreeMode(directory, mode) {
  const info = fs.lstatSync(directory);
  if (info.isDirectory() && !info.isSymbolicLink()) {
    for (const name of fs.readdirSync(directory)) setTreeMode(path.join(directory, name), mode);
  }
  fs.chmodSync(directory, mode);
}

test('worker dependency builds use a writable managed copy of the verified read-only release sources', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-worker-source-staging-'));
  const app = path.join(root, 'release'); const data = path.join(root, 'data');
  const profile = path.join(app, 'workers/dataset/runtime-profiles/linux-x86_64-cpu');
  const manager = path.join(app, 'packages/model_manager');
  fs.mkdirSync(profile, { recursive: true }); fs.mkdirSync(manager, { recursive: true }); fs.mkdirSync(data);
  fs.writeFileSync(path.join(profile, 'pyproject.toml'), '[project]\nname="cpu-profile"\n');
  fs.writeFileSync(path.join(profile, 'uv.lock'), 'locked recipe\n');
  fs.writeFileSync(path.join(manager, 'pyproject.toml'), '[project]\nname="speechcraft-model-manager"\n');
  fs.writeFileSync(path.join(manager, 'README.md'), 'packaged local dependency\n');
  fs.mkdirSync(path.join(manager, 'speechcraft_models'));
  fs.writeFileSync(path.join(manager, 'speechcraft_models/cli.py'), 'value = 1\n');
  setTreeMode(app, 0o555);
  try {
    const stagedProfile = stageWorkerBuildSources(app, data, path.join(data, 'staging/attempt-test'),
      'workers/dataset/runtime-profiles/linux-x86_64-cpu');
    const stagedManager = path.join(data, 'staging/attempt-test/worker-source/packages/model_manager');
    const stagedWorker = path.join(data, 'staging/attempt-test/worker-source/workers/dataset');
    assert.equal(path.join(stagedProfile, '../../../../packages/model_manager'), stagedManager);
    assert.equal(path.join(stagedProfile, '../..'), stagedWorker);
    assert.ok(fs.statSync(stagedManager).mode & 0o200, 'the local dependency directory must be writable by uv');
    assert.ok(fs.statSync(path.join(stagedManager, 'pyproject.toml')).mode & 0o200, 'the build metadata must be writable');
    assert.equal(fs.readFileSync(path.join(stagedManager, 'speechcraft_models/cli.py'), 'utf8'), 'value = 1\n');
    assert.equal(fs.statSync(path.join(manager, 'pyproject.toml')).mode & 0o222, 0, 'the installed release must remain read-only');
    assert.equal(fs.readFileSync(path.join(manager, 'pyproject.toml'), 'utf8'), '[project]\nname="speechcraft-model-manager"\n');
  } finally {
    setTreeMode(app, 0o700);
    fs.rmSync(root, { recursive: true, force: true });
  }
});

const pythonProgram = String.raw`#!/usr/bin/env node
const path = require('node:path');
const source = process.argv[process.argv.indexOf('-c') + 1] || '';
const executable = process.argv[1];
const runtime = process.env.UV_PYTHON_INSTALL_DIR;
if (source.includes('SPEECHCRAFT_PYTHON_PROBE')) {
  console.log(JSON.stringify({ kind: 'SPEECHCRAFT_PYTHON_PROBE', version: '3.12.15', machine: 'x86_64', system: 'Linux', executable, prefix: runtime, base_prefix: runtime }));
} else if (source.includes('SPEECHCRAFT_MODEL_MANAGER_PROBE')) {
  console.log(JSON.stringify({ kind: 'SPEECHCRAFT_MODEL_MANAGER_PROBE', version: '0.1.0', roles: ['asr', 'diarization', 'speaker_purity', 'speech_detection'], catalog_valid: true, python: '3.12.15', prefix: process.env.SPEECHCRAFT_EXPECTED_MANAGER_PREFIX, base_prefix: process.env.SPEECHCRAFT_EXPECTED_MANAGED_PYTHON_ROOT, heavy_dependencies: [], executable, wheel_sha256: process.env.SPEECHCRAFT_EXPECTED_MODEL_MANAGER_WHEEL_SHA256 || null }));
} else if (process.argv.includes('--help')) {
  console.log('speechcraft_models install verify status');
} else {
  process.exitCode = 3;
}`;

const uvProgram = String.raw`#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
const py = ${JSON.stringify(pythonProgram)};
const executable = path.join(process.env.UV_PYTHON_INSTALL_DIR, 'bin', 'python');
if (args[0] === '--version') console.log('uv 0.0.1-test');
else if (args[0] === 'python' && args[1] === 'find') {
  if (fs.existsSync(executable)) console.log(executable); else process.exitCode = 1;
} else if (args[0] === 'python' && args[1] === 'install') {
  fs.mkdirSync(path.dirname(executable), { recursive: true }); fs.writeFileSync(executable, py, { mode: 0o755 }); fs.chmodSync(executable, 0o755);
} else if (args[0] === 'venv') {
  const target = args.at(-1); const envPython = path.join(target, 'bin', 'python');
  fs.mkdirSync(path.dirname(envPython), { recursive: true }); fs.writeFileSync(envPython, py, { mode: 0o755 }); fs.chmodSync(envPython, 0o755);
} else if (args[0] === 'pip' && ['sync', 'install'].includes(args[1])) { /* deterministic stand-in */ }
else process.exitCode = 2;`;

function testManifest(archive, uvBytes, requirementsBytes) {
  return {
    uv: { version: '0.0.1-test', artifacts: { 'linux-x86_64': {
      file: 'uv-test.tar.gz', url: null, size_bytes: archive.length, sha256: hash(archive),
      tar_root: 'fixture-uv', executable: 'uv', executable_size_bytes: uvBytes.length,
      executable_sha256: hash(uvBytes),
    } } },
    python: { version: '3.12.15', build: 'fixture', supported_profiles: { 'linux-x86_64': { sha256: 'a'.repeat(64), size_bytes: 1234 } } },
    model_manager: { version: '0.1.0', dependency_lock: 'requirements.txt', requirements_sha256: hash(requirementsBytes) },
    disk_space: { safety_margin_bytes: 1, estimated_python_environment_bytes: 1,
      estimated_model_manager_environment_and_cache_bytes: 1 },
  };
}

test('uv extraction accepts only the expected plain files and rejects archive links', () => {
  const uv = Buffer.from(uvProgram);
  const artifact = { tar_root: 'fixture-uv', executable_size_bytes: uv.length };
  assert.deepEqual(extractUvArchive(archiveFor({ uv, uvx: 'unused' }), artifact), uv);
  assert.throws(() => extractUvArchive(gzipSync(Buffer.concat([
    tarEntry('fixture-uv/', Buffer.alloc(0), '5'), tarEntry('fixture-uv/uv', uv), tarEntry('fixture-uv/uvx', Buffer.from('x'), '2'), Buffer.alloc(1024),
  ])), artifact), (error) => error.code === 'UV_ARCHIVE_UNSAFE');
});

test('real Node bootstrap orchestration installs and reuses uv, managed Python, and the isolated model manager', { skip: process.platform !== 'linux' || process.arch !== 'x64' }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-bootstrap-integration-'));
  const uvBytes = Buffer.from(uvProgram);
  const archive = archiveFor({ uv: uvBytes, uvx: Buffer.from('stand-in uvx') });
  const server = http.createServer((_request, response) => {
    server.downloads += 1;
    response.writeHead(200, { 'content-length': archive.length, 'content-type': 'application/gzip' });
    response.end(archive);
  });
  server.downloads = 0;
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const profile = 'linux-x86_64';
  const requirementsBytes = Buffer.from('# tiny offline fixture\n');
  const manifest = testManifest(archive, uvBytes, requirementsBytes);
  manifest.uv.artifacts[profile].url = `http://127.0.0.1:${server.address().port}/uv`;
  const wheel = path.join(root, 'speechcraft_model_manager-0.1.0-py3-none-any.whl');
  fs.writeFileSync(wheel, 'offline test wheel');
  const requirements = path.join(root, 'requirements.txt');
  fs.writeFileSync(requirements, requirementsBytes);
  const tasks = new BootstrapTasks({ dataDirectory: root, wheelPath: wheel, wheelSha256: hash(fs.readFileSync(wheel)),
    bootstrapManifest: manifest, requirementsPath: requirements, availableBytes: 2 * 1024 ** 3 });
  const store = StateStore.open(root);
  const controller = new SetupController(store, { tasks, mode: 'bootstrap' });
  try {
    controller.start();
    await controller.waitForIdle();
    let job = store.snapshot().job;
    assert.equal(job.state, 'completed', JSON.stringify(job));
    assert.equal(job.bootstrap_status.uv_verified, true);
    assert.equal(job.bootstrap_status.python_verified, true);
    assert.equal(job.bootstrap_status.model_manager_verified, true);
    assert.equal(job.bootstrap_status.bootstrap_ready, true);
    assert.equal(job.bootstrap_status.production_ready, false);
    assert.equal(server.downloads, 1);
    const installedPython = job.bootstrap_status.python_executable;
    assert.ok(fs.existsSync(installedPython));

    tasks.availableBytesOverride = 1;
    controller.start();
    await controller.waitForIdle();
    job = store.snapshot().job;
    assert.equal(job.state, 'completed', JSON.stringify(job.error));
    assert.equal(job.bootstrap_status.bootstrap_ready, true);
    assert.equal(job.bootstrap_status.production_ready, false);
    assert.equal(server.downloads, 1, 'the verified uv archive must be reused');
  } finally {
    await new Promise((resolve) => server.close(resolve));
    store.releaseOwnership();
  }
});

test('bootstrap checks capacity before acquisition but allows reuse below the install estimate', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-bootstrap-disk-'));
  const archive = Buffer.from('archive fixture');
  const uvBytes = Buffer.from('binary fixture');
  const requirementsBytes = Buffer.from('');
  const manifest = testManifest(archive, uvBytes, requirementsBytes);
  const tasks = new BootstrapTasks({ dataDirectory: root,
    wheelPath: path.join(root, 'speechcraft_model_manager-0.1.0-py3-none-any.whl'),
    wheelSha256: 'a'.repeat(64), bootstrapManifest: manifest, requirementsPath: path.join(root, 'requirements.txt'),
    availableBytes: 2 * 1024 ** 3 });
  fs.writeFileSync(tasks.requirementsPath, requirementsBytes);
  const context = { attemptId: '00000000-0000-4000-8000-000000000000',
    signal: new AbortController().signal, onProgress() {}, onComponentStatus() {} };
  await tasks.run('inspect_system', context);
  tasks.availableBytesOverride = 1;
  await assert.rejects(tasks.run('install_uv', context),
  (error) => error.code === 'INSUFFICIENT_DISK_SPACE');
});

test('ordinary installed-runtime recovery requires and binds reuse to the trusted shipped wheel', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-manager-recovery-'));
  const wheel = path.join(root, 'speechcraft_model_manager-0.1.0-py3-none-any.whl');
  fs.writeFileSync(wheel, 'trusted wheel fixture');
  const wheelSha = hash(fs.readFileSync(wheel));
  const tasks = new BootstrapTasks({ dataDirectory: root, wheelPath: wheel, wheelSha256: wheelSha,
    processRunner: async (_exe, _args, options) => ({ code: 0, stdout: JSON.stringify({ kind: 'SPEECHCRAFT_MODEL_MANAGER_PROBE', version: '0.1.0',
      roles: ['diarization', 'asr', 'speech_detection', 'speaker_purity'], catalog_valid: true, python: '3.12.15', heavy_dependencies: [],
      prefix: options.env.SPEECHCRAFT_EXPECTED_MANAGER_PREFIX,
      base_prefix: options.env.SPEECHCRAFT_EXPECTED_MANAGED_PYTHON_ROOT, wheel_sha256: wheelSha }) }) });
  tasks.environment = { profile: 'linux-x86_64' };
  tasks.paths = tasks.getPaths('linux-x86_64');
  fs.mkdirSync(tasks.paths.pythonRoot, { recursive: true });
  const version = tasks.manifest.python.version;
  const lockPrefix = tasks.manifest.model_manager.requirements_sha256.slice(0, 12);
  const target = path.join(root, 'environments', 'speechcraft-model-manager',
    `0.1.0-py${version}-linux-x86_64-${wheelSha.slice(0, 12)}-${lockPrefix}`);
  assert.equal(target, tasks.paths.environmentRoot);
  fs.mkdirSync(path.join(target, 'bin'), { recursive: true, mode: 0o700 });
  const python = path.join(target, 'bin', 'python');
  fs.writeFileSync(python, '#!/bin/sh\nexit 0\n', { mode: 0o700 });
  const found = await tasks.findInstalledModelManager({ attemptId: 'fixture', signal: new AbortController().signal,
    onChildProcess: () => true });
  assert.equal(found.environmentRoot, target);
  assert.equal(found.python, python);
  assert.equal(found.wheel_sha256, wheelSha);
  const noWheel = new BootstrapTasks({ dataDirectory: root, wheelPath: undefined, wheelSha256: undefined });
  noWheel.environment = { profile: 'linux-x86_64' };
  noWheel.paths = noWheel.getPaths('linux-x86_64', { allowMissingManager: true });
  await assert.rejects(noWheel.findInstalledModelManager({ attemptId: 'fixture', signal: new AbortController().signal }),
    (error) => error.code === 'MODEL_MANAGER_WHEEL_REQUIRED');
  fs.rmSync(root, { recursive: true, force: true });
});

test('controller restart reconstructs all installed model paths and retains the prior CUDA selection for revalidation without a wheel', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-full-runtime-recovery-'));
  const firstStore = StateStore.open(root);
  const firstController = new SetupController(firstStore, { mode: 'simulated' });
  firstController.start(); await firstController.waitForIdle();
  firstStore.commit((state) => {
    state.job.mode = 'runtime'; state.job.plan_id = 'speechcraft-runtime-v1';
    state.job.bootstrap_status = { runtime_profile: { profile: 'linux-x86_64', workerProfile: 'linux-x86_64-cuda',
      modelPlatform: 'linux-x86_64', accelerator: 'cuda', cuda_validated: true,
      cuda_probe: { schema_version: 1, component: 'cuda', success: true } },
    host_details: { cuda_candidate: true, nvidia_gpus: ['fixture GPU'] }, components: {} };
  }, 'install_fixture_for_restart_recovery');
  await firstController.shutdown(); firstStore.releaseOwnership();

  const store = StateStore.open(root);
  const managerRoot = path.join(root, 'environments', 'speechcraft-model-manager', 'fixture-manager');
  fs.mkdirSync(path.join(managerRoot, 'bin'), { recursive: true });
  const expectedWheelSha = 'c'.repeat(64);
  const reports = {
    speech_detection: { install_ready: true, integrity_verified: true, root: path.join(root, 'models', 'silero') },
    asr: { install_ready: true, integrity_verified: true, root: path.join(root, 'models', 'whisper') },
    speaker_purity: { install_ready: false, integrity_verified: true, complete: true,
      release_blockers: ['TitaNet provenance unresolved'], root: path.join(root, 'models', 'titanet') },
    diarization: { install_ready: true, integrity_verified: true, inventory_exact: true, root: path.join(root, 'models', 'pyannote') },
  };
  const tasks = new BootstrapTasks({ dataDirectory: root, wheelPath: undefined, wheelSha256: undefined,
    processRunner: async (_exe, args) => {
      const role = args[args.indexOf('status') + 1];
      return { code: reports[role] ? 0 : 1, stdout: JSON.stringify(reports[role] || {}), stderr: '' };
    } });
  tasks.verifyUv = async () => true;
  tasks.findPython = async () => path.join(root, 'runtimes', 'python', 'bin', 'python');
  tasks.verifyPython = async () => ({ version: '3.12.15' });
  tasks.findInstalledModelManager = async () => ({ environmentRoot: managerRoot,
    python: path.join(managerRoot, 'bin', 'python'), version: '0.1.0', wheel_sha256: expectedWheelSha });
  tasks.validateBackendEnvironment = async () => true;
  try {
    await tasks.recoverInterrupted(store);
    const status = store.snapshot().job.bootstrap_status;
    assert.equal(status.model_manager_verified, true);
    assert.equal(status.components.model_manager.path, managerRoot);
    assert.equal(status.components.backend_environment.state, 'verified');
    assert.equal(status.runtime_profile.workerProfile, 'linux-x86_64-cuda', 'the prior profile is retained for real inference revalidation');
    for (const [component, rootName] of [['silero', 'silero'], ['whisper', 'whisper'], ['titanet', 'titanet'], ['pyannote', 'pyannote']]) {
      assert.equal(status.components[component].path, path.join(root, 'models', rootName));
      assert.equal(status.components[component].runtime_ready, false, 'old inference readiness is never blindly trusted');
    }
    assert.equal(status.components.titanet.release_approved, false);
    assert.match(status.components.titanet.message, /provenance/i);
  } finally {
    store.releaseOwnership();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('an owned bootstrap subprocess that ignores SIGTERM is killed and reaped before cancellation settles', { skip: process.platform === 'win32' }, async () => {
  const abort = new AbortController();
  const started = Date.now();
  const promise = runOwnedProcess(process.execPath, ['-e',
    "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);"],
  { signal: abort.signal, cwd: os.tmpdir(), env: { PATH: process.env.PATH }, graceMs: 80 });
  setTimeout(() => abort.abort(), 50);
  await assert.rejects(promise, (error) => error instanceof BootstrapError && error.code === 'BOOTSTRAP_CANCELLED');
  assert.ok(Date.now() - started >= 50);
  assert.ok(Date.now() - started < 2_000);
});

test('initial SIGTERM is withheld when the recorded process identity changes', { skip: process.platform === 'win32' }, async () => {
  const abort = new AbortController();
  let observed = null;
  const operation = runOwnedProcess(process.execPath, ['-e', 'setInterval(() => {},1000)'], {
    signal: abort.signal, cwd: os.tmpdir(), env: { PATH: process.env.PATH }, graceMs: 40,
    onChildProcess(record) {
      if (record?.clear) return;
      observed = { pid: record.pid, identity: record.identity };
      record.identity = 'deliberately-changed-identity';
    },
  });
  try {
    const deadline = Date.now() + 2_000;
    while (!observed && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    assert.ok(observed);
    abort.abort();
    await assert.rejects(operation, { code: 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN' });
    assert.doesNotThrow(() => process.kill(observed.pid, 0), 'identity mismatch must not signal the possibly reused process group');
  } finally {
    if (observed) { try { process.kill(observed.pid, 'SIGKILL'); } catch {} }
  }
});

test('an exited leader with a SIGTERM-ignoring descendant retains uncertain ownership', { skip: process.platform === 'win32' }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-owned-descendant-'));
  const pidFile = path.join(root, 'descendant.pid');
  const readyFile = path.join(root, 'descendant.ready');
  const store = StateStore.open(root);
  const attemptId = '00000000-0000-4000-8000-000000000002';
  store.commit((state) => { state.job = { schema_version: 2, id: '00000000-0000-4000-8000-000000000001',
    attempt_id: attemptId, mode: 'bootstrap', state: 'running', stage: 'install_python',
    task_id: 'bootstrap:install_python', created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    started_at: new Date().toISOString(), completed_at: null, attempt_count: 1, revision: 0,
    progress: null, bootstrap_status: null, error: null }; }, 'test_bootstrap_running');
  const abort = new AbortController();
  const updates = [];
  const childScript = `
    (async () => {
    const fs = require('node:fs');
    const { spawn } = require('node:child_process');
    const child = spawn(process.execPath, ['-e', "const fs=require('node:fs'); process.on('SIGTERM', () => {}); fs.writeFileSync(process.argv[1], 'ready'); setInterval(() => {}, 1000);", ${JSON.stringify(readyFile)}], { stdio: 'ignore' });
    const deadline = Date.now() + 2000;
    while (!fs.existsSync(${JSON.stringify(readyFile)}) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    fs.writeFileSync(${JSON.stringify(pidFile)}, String(child.pid));
    setInterval(() => {}, 1000);
    })();
  `;
  const operation = runOwnedProcess(process.execPath, ['-e', childScript], {
    signal: abort.signal, cwd: root, env: process.env, graceMs: 100,
    onChildProcess: (record) => {
      updates.push(record);
      if (record?.clear) store.commit((state) => { state.job.child_process = null; }, 'test_owner_cleared');
      else if (record) store.commit((state) => { state.job.child_process = { ...record, attempt_id: attemptId }; }, 'test_owner_updated');
    },
  });
  try {
    const deadline = Date.now() + 3_000;
    while (!fs.existsSync(pidFile) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(fs.existsSync(pidFile), true, 'descendant should be running before cancellation');
    abort.abort();
    await assert.rejects(operation, (error) => error.code === 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN');
    assert.ok(updates.some((item) => item?.termination_status === 'termination_uncertain'));
    assert.equal(updates.some((item) => item?.clear), false, 'ownership must not be cleared on uncertain termination');
    assert.equal(store.snapshot().job.child_process.termination_status, 'termination_uncertain');
    await assert.rejects(reconcileOwnedProcess(store), (error) => error.code === 'BOOTSTRAP_PROCESS_IDENTITY_UNKNOWN');
    assert.equal(store.snapshot().job.child_process.termination_status, 'termination_uncertain',
      'failed restart reconciliation must preserve ownership metadata');
  } finally {
    if (fs.existsSync(pidFile)) {
      const pid = Number(fs.readFileSync(pidFile, 'utf8'));
      try { process.kill(pid, 'SIGKILL'); } catch {}
    }
    store.releaseOwnership();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
