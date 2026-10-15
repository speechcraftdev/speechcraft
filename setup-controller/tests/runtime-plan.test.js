import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { assertTrustedBackendRecipe, cudaProfileReady, initialComponents, runtimeManifest, selectRuntimeProfile } from '../src/runtime-plan.js';
import { RuntimeTasks } from '../src/runtime-tasks.js';

const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

test('platform selection keeps WSL storage host identity separate from model artifact selection', () => {
  assert.equal(selectRuntimeProfile({ os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true }).modelPlatform, 'windows-wsl2');
  assert.equal(selectRuntimeProfile({ os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false }).modelPlatform, 'macos-arm64');
  assert.throws(() => selectRuntimeProfile({ profile: 'windows-x64', os: 'windows', architecture: 'x86_64' }), { code: 'RUNTIME_PROFILE_UNAVAILABLE' });
});

test('CUDA remains a candidate until its recipe and independent Torch/CTranslate2 probes pass', () => {
  const host = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: false };
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true }).workerProfile, 'linux-x86_64-cpu');
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true, cudaRecipeAvailable: true, cudaValidated: true,
    cudaProbe: { success: true, torch: true, ctranslate2: true } }).workerProfile, 'linux-x86_64-cpu',
  'a CUDA profile marked unavailable in the trusted catalog cannot be selected');
  const fixture = structuredClone(runtimeManifest);
  fixture.worker.profiles['linux-x86_64-cuda'].available = true;
  fixture.worker.profiles['linux-x86_64-cuda'].validated = true;
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], true,
    { success: true, torch: true, ctranslate2: true }), true, 'all runtime conditions must be present');
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], true,
    { success: true, torch: true, ctranslate2: false }), false);
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], false,
    { success: true, torch: true, ctranslate2: true }), false);
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true, cudaRecipeAvailable: true, cudaValidated: true,
    cudaProbe: { success: true, torch: true, ctranslate2: false } }).workerProfile, 'linux-x86_64-cpu');
});

test('checked-in backend recipe identity is verified and unsupported components remain blocked', () => {
  const root = path.resolve('..');
  assert.ok(assertTrustedBackendRecipe(root).lock.endsWith(path.join('backend', 'uv.lock')));
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cpu'].available, true);
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cpu'].validated, true);
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cuda'].validated, false);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].available, false);
  assert.equal(initialComponents().pyannote.state, 'blocked');
  assert.equal(initialComponents().titanet.state, 'blocked');
  assert.equal(initialComponents().runtime_probes.state, 'pending');
  assert.equal(runtimeManifest.runtime_validation.available, true);
  for (const probe of runtimeManifest.runtime_validation.probes) {
    assert.match(probe.environment_role, /^(worker|backend|model_manager)$/);
    assert.equal(sha(path.resolve(root, probe.helper)), probe.sha256, `trusted helper hash: ${probe.component}`);
  }
});

test('runtime executor reports an explicitly unavailable worker recipe without pretending it was installed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-plan-'));
  const recipe = structuredClone(runtimeManifest);
  recipe.worker.profiles['linux-x86_64-cpu'].available = false;
  recipe.worker.profiles['linux-x86_64-cpu'].reason = 'fixture blocked';
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe: recipe });
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
  const sourceProject = path.join(root, 'workers', 'dataset');
  fs.mkdirSync(sourceProject, { recursive: true });
  fs.writeFileSync(path.join(sourceProject, 'pyproject.toml'), '[project]\nname="source-fixture"\n');
  const lock = path.join(project, 'uv.lock'); const manifestFile = path.join(project, 'pyproject.toml');
  fs.writeFileSync(lock, 'fixture lock'); fs.writeFileSync(manifestFile, '[project]\nname="worker-fixture"\n');
  const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.worker.source_manifest_sha256 = sha(path.join(sourceProject, 'pyproject.toml'));
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
  context.componentStatus.components.worker_environment = { state: 'verified', path: path.join(root, 'environments', 'worker', 'v1') };
  const managerPython = path.join(root, 'environments', 'model-manager', 'bin', 'python');
  fs.mkdirSync(path.dirname(managerPython), { recursive: true });
  fs.writeFileSync(managerPython, 'fixture interpreter');
  tasks.getManagerPython = () => managerPython;
  await tasks.run('validate_runtime', context);
  assert.equal(context.componentStatus.components.silero.state, 'runtime_ready');
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('runtime probe failures remain component blockers and do not hide later successful probes', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-probe-blockers-'));
  const failed = path.join(root, 'failed.py'); const good = path.join(root, 'good.py');
  fs.writeFileSync(failed, '# trusted failing fixture'); fs.writeFileSync(good, '# trusted successful fixture');
  const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const workerPython = path.join(root, 'environments', 'worker', 'v1', 'bin', 'python');
  fs.mkdirSync(path.dirname(workerPython), { recursive: true }); fs.writeFileSync(workerPython, 'fixture interpreter');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.runtime_validation = { available: true, probes: [
    { component: 'ffmpeg', helper: 'failed.py', sha256: sha(failed), environment_role: 'worker', arguments: ['ffmpeg'], timeout_ms: 2000 },
    { component: 'silero', helper: 'good.py', sha256: sha(good), environment_role: 'worker', arguments: ['silero'], timeout_ms: 2000 },
  ] };
  const calls = [];
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    processRunner: async (executable) => {
      calls.push(executable);
      return calls.length === 1
        ? { code: 1, stdout: JSON.stringify({ schema_version: 1, component: 'ffmpeg', success: false, inference_completed: false, error_code: 'runtime_tool_unavailable', error: 'Managed FFmpeg is absent.' }), stderr: '' }
        : { code: 0, stdout: JSON.stringify({ schema_version: 1, component: 'silero', success: true,
          inference_completed: true, identity: 'silero-fixture', device: 'cpu' }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const context = { attemptId: '00000000-0000-4000-8000-000000000099', signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(), onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  context.componentStatus.components.worker_environment = { state: 'verified', path: path.join(root, 'environments', 'worker', 'v1') };
  context.bootstrapStatus = context.componentStatus;
  await tasks.run('validate_runtime', context);
  assert.deepEqual(calls, [workerPython, workerPython]);
  assert.equal(context.componentStatus.components.ffmpeg.state, 'blocked');
  assert.equal(context.componentStatus.components.silero.state, 'runtime_ready');
  assert.deepEqual(context.componentStatus.components.runtime_probes.blocked_components, ['ffmpeg']);
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
