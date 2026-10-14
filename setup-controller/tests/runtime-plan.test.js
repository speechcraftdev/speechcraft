import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { assertTrustedBackendRecipe, initialComponents, runtimeManifest, selectRuntimeProfile } from '../src/runtime-plan.js';
import { RuntimeTasks } from '../src/runtime-tasks.js';

test('platform selection keeps WSL storage host identity separate from model artifact selection', () => {
  assert.equal(selectRuntimeProfile({ os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true }).modelPlatform, 'windows-wsl2');
  assert.equal(selectRuntimeProfile({ os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false }).modelPlatform, 'macos-arm64');
  assert.throws(() => selectRuntimeProfile({ profile: 'windows-x64', os: 'windows', architecture: 'x86_64' }), { code: 'RUNTIME_PROFILE_UNAVAILABLE' });
});

test('checked-in backend recipe identity is verified and unsupported components remain blocked', () => {
  const root = path.resolve('..');
  assert.ok(assertTrustedBackendRecipe(root).lock.endsWith(path.join('backend', 'uv.lock')));
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cpu'].available, false);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].available, false);
  assert.equal(initialComponents().pyannote.state, 'blocked');
  assert.equal(initialComponents().titanet.state, 'blocked');
  assert.equal(initialComponents().runtime_probes.failure_code, 'runtime_validation_pending');
});

test('runtime executor reports the absent CPU worker recipe without pretending it was installed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-plan-'));
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  const context = { componentStatus: tasks.initialBootstrapStatus(), onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_worker_environment', context);
  assert.equal(context.componentStatus.components.worker_environment.state, 'blocked');
  assert.equal(context.componentStatus.components.worker_environment.failure_code, 'runtime_profile_unavailable');
  assert.equal(context.componentStatus.components.dataset_processing.state, 'blocked');
  fs.rmSync(root, { recursive: true, force: true });
});

test('worker installer consumes only a complete trusted profile recipe and validates its isolated environment', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-worker-recipe-'));
  const project = path.join(root, 'worker_fixture');
  fs.mkdirSync(project);
  const lock = path.join(project, 'uv.lock'); const manifestFile = path.join(project, 'pyproject.toml');
  fs.writeFileSync(lock, 'fixture lock'); fs.writeFileSync(manifestFile, '[project]\nname="worker-fixture"\n');
  const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.worker.profiles['linux-x86_64-cpu'] = { available: true, validated: true,
    project: 'worker_fixture', lockfile: 'uv.lock', lock_sha256: sha(lock), project_manifest_sha256: sha(manifestFile),
    environment_version: 1, python_version: '3.12.15', estimated_bytes: 1, validation_modules: ['json'] };
  runtimeRecipe.disk_space.safety_margin_bytes = 1;
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    availableBytes: 1024, processRunner: async (executable, args, options) => {
      if (args[0] === 'sync') {
        const target = options.env.UV_PROJECT_ENVIRONMENT;
        fs.mkdirSync(path.join(target, 'bin'), { recursive: true });
        fs.writeFileSync(path.join(target, 'bin', 'python'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
        return { code: 0, stdout: '', stderr: '' };
      }
      return { code: 0, stdout: JSON.stringify({ kind: 'SPEECHCRAFT_WORKER_ENV_PROBE', profile: 'linux-x86_64-cpu', missing: [], python: args[0] }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { uvExecutable: '/trusted/uv', pythonRoot: root, home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const context = { attemptId: '00000000-0000-4000-8000-000000000099', managedPython: '/managed/python',
    signal: new AbortController().signal, componentStatus: tasks.initialBootstrapStatus(),
    onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_worker_environment', context);
  assert.equal(context.componentStatus.components.worker_environment.state, 'verified');
  assert.equal(context.componentStatus.components.worker_environment.verification, 'trusted_lock_and_import_probe');
  assert.equal(context.componentStatus.components.dataset_processing.state, 'blocked');
  fs.rmSync(root, { recursive: true, force: true });
});

test('runtime probe runner requires a trusted helper and explicit completed inference result', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-probe-'));
  const helper = path.join(root, 'probe.py'); fs.writeFileSync(helper, '# trusted fixture');
  const sha = createHash('sha256').update(fs.readFileSync(helper)).digest('hex');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.runtime_validation = { available: true, probes: [{ component: 'silero', helper: 'probe.py', sha256: sha,
    timeout_ms: 2000, arguments: [] }] };
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    processRunner: async () => ({ code: 0, stdout: JSON.stringify({ schema_version: 1, component: 'silero',
      success: true, inference_completed: true, identity: 'silero-fixture', device: 'cpu' }), stderr: '' }) });
  tasks.environment = { profile: 'linux-x86_64', wsl: false };
  tasks.paths = { home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const context = { attemptId: '00000000-0000-4000-8000-000000000099', signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(), onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  tasks.getManagerPython = () => '/managed/python';
  await tasks.run('validate_runtime', context);
  assert.equal(context.componentStatus.components.silero.state, 'runtime_ready');
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('backend environment stage installs from the checked-in lock and reuses a verified target', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-backend-env-stage-'));
  let syncs = 0;
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: path.resolve('..'), availableBytes: 8 * 1024 ** 3,
    processRunner: async (_executable, args, options) => {
      if (args[0] === 'sync') { syncs += 1; assert.ok(args.includes('--locked')); assert.equal(options.env.UV_PROJECT_ENVIRONMENT, tasks.backendEnvironmentPath());
        fs.mkdirSync(path.join(options.env.UV_PROJECT_ENVIRONMENT, 'bin'), { recursive: true });
        fs.writeFileSync(path.join(options.env.UV_PROJECT_ENVIRONMENT, 'bin', 'python'), 'fixture');
        return { code: 0, stdout: '', stderr: '' }; }
      const code = args.at(-1);
      const kind = code.match(/'kind':'([A-Z_]+)'/)?.[1];
      return { code: 0, stdout: JSON.stringify({ kind, python: '/managed/backend/bin/python', version: '3.12.15' }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvExecutable: '/trusted/uv', uvCache: root, pythonRoot: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const context = { attemptId: '00000000-0000-4000-8000-000000000100', managedPython: '/managed/python',
    signal: new AbortController().signal, componentStatus: tasks.initialBootstrapStatus(),
    onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_backend_environment', context);
  await tasks.run('install_backend_environment', context);
  assert.equal(syncs, 1, 'the verified environment should be reused on the second pass');
  assert.equal(context.componentStatus.components.backend_environment.state, 'verified');
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('model stage invokes the existing model-manager contract for platform-selected required roles', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-model-stage-'));
  const calls = [];
  const tasks = new RuntimeTasks({ dataDirectory: root, availableBytes: 32 * 1024 ** 3,
    processRunner: async (_python, args) => {
      calls.push(args);
      const role = args[args.indexOf('install') + 1];
      return { code: 0, stdout: JSON.stringify({ status: 'verified', install_ready: true, integrity_verified: true,
        role, files: [{ path: 'tiny-fixture', status: 'verified' }] }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: true, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvCache: root, hfHome: path.join(root, 'hf'), hfHub: path.join(root, 'hf', 'hub'),
    hfXet: path.join(root, 'hf', 'xet'), temporary: path.join(root, 'tmp'), pythonRoot: root };
  tasks.getManagerPython = () => '/managed/model-manager/python';
  const context = { attemptId: '00000000-0000-4000-8000-000000000101', signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(), onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_models', context);
  assert.deepEqual(calls.map((args) => args[args.indexOf('install') + 1]), ['speech_detection', 'asr']);
  assert.ok(calls.every((args) => args.includes('--platform') && args[args.indexOf('--platform') + 1] === 'windows-wsl2'));
  assert.equal(context.componentStatus.components.silero.state, 'files_verified');
  assert.equal(context.componentStatus.components.whisper.state, 'files_verified');
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});
