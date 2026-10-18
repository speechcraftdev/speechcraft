import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { BackendService } from '../src/backend-service.js';
import { assertTrustedBackendRecipe, cudaProfileReady, evaluateRuntimeReadiness, initialComponents, runtimeManifest, selectRuntimeProfile } from '../src/runtime-plan.js';
import { RuntimeTasks } from '../src/runtime-tasks.js';

const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');

test('managed backend passes only the verified ECAPA model root and ONNX path to dataset workers', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-ecapa-worker-environment-'));
  const originalStart = BackendService.prototype.start;
  try {
    let environmentVars;
    let startCalls = 0;
    BackendService.prototype.start = async function startForTest() {
      startCalls += 1;
      environmentVars = this.environmentVars;
      return { port: 8010 };
    };
    const tasks = new RuntimeTasks({ dataDirectory: root });
    tasks.environment = { profile: 'linux-x86_64', os: 'linux', architecture: 'x86_64', wsl: false };
    tasks.paths = { home: root, uvCache: root, pythonRoot: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
    tasks.backendEnvironmentPath = () => path.join(root, 'environments', 'backend', 'bin', 'python');
    const modelRoot = path.join(root, 'models');
    const components = {
      backend_environment: { state: 'verified' },
      worker_environment: { state: 'verified', path: path.join(root, 'environments', 'worker'), library_paths: [] },
      ffmpeg: { state: 'runtime_ready', path: path.join(root, 'tools', 'ffmpeg') },
      silero: { state: 'runtime_ready', path: path.join(modelRoot, 'silero') },
      whisper: { state: 'runtime_ready', path: path.join(modelRoot, 'whisper') },
      pyannote: { state: 'runtime_ready', path: path.join(modelRoot, 'community-1') },
      ecapa: { state: 'runtime_ready', path: path.join(modelRoot, 'wespeaker-ecapa-tdnn512-lm') },
    };
    await tasks.startServices({ attemptId: 'ecapa-env-test', signal: new AbortController().signal,
      componentStatus: { components, runtime_profile: { accelerator: 'cuda' } } });
    assert.equal(environmentVars.SPEECHCRAFT_ECAPA_MODEL_PATH, components.ecapa.path);
    assert.equal(environmentVars.SPEECHCRAFT_ECAPA_ONNX_PATH,
      path.join(components.ecapa.path, 'voxceleb_ECAPA512_LM.onnx'));

    // A stale or unverified component must not provide a worker model path.
    components.ecapa.state = 'files_verified';
    tasks.service = null;
    const unverifiedStatus = { components, runtime_profile: { accelerator: 'cuda' } };
    await tasks.startServices({ attemptId: 'ecapa-env-unverified-test', signal: new AbortController().signal,
      componentStatus: unverifiedStatus });
    assert.equal(startCalls, 1, 'the backend cannot start without verified ECAPA inference');
    assert.equal(unverifiedStatus.components.backend_service.failure_code, 'runtime_prerequisite_unavailable');
  } finally {
    BackendService.prototype.start = originalStart;
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('platform selection keeps WSL storage host identity separate from model artifact selection', () => {
  assert.equal(selectRuntimeProfile({ os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true }).modelPlatform, 'windows-wsl2');
  assert.equal(selectRuntimeProfile({ os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false }).modelPlatform, 'macos-arm64');
  assert.throws(() => selectRuntimeProfile({ profile: 'windows-x64', os: 'windows', architecture: 'x86_64' }), { code: 'RUNTIME_PROFILE_UNAVAILABLE' });
});

test('CUDA remains a candidate until its recipe and independent Torch/CTranslate2 probes pass', () => {
  const host = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: false };
  const passingCuda = { schema_version: 1, component: 'cuda', success: true, inference_completed: true,
    gpu_detected: true, driver_identification: 'fixture driver', actual_execution_device: 'fixture GPU',
    required_libraries: { cublas: true, cudnn: true, cudart: true },
    torch_cuda_success: true, ctranslate2_cuda_success: true, measurements: { whisper_segments: 1 } };
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true }).workerProfile, 'linux-x86_64-cpu');
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true, cudaRecipeAvailable: true, cudaValidated: true,
    cudaProbe: passingCuda }).workerProfile, 'linux-x86_64-cuda');
  const fixture = structuredClone(runtimeManifest);
  fixture.worker.profiles['linux-x86_64-cuda'].available = true;
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], true, passingCuda), true,
    'actual inference and both library stacks must pass');
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], true,
    { ...passingCuda, ctranslate2_cuda_success: false }), false);
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], false,
    passingCuda), false);
  assert.equal(cudaProfileReady(fixture.worker.profiles['linux-x86_64-cuda'], true,
    { ...passingCuda, required_libraries: { cublas: true, cudnn: false, cudart: true } }), false);
  assert.equal(selectRuntimeProfile(host, { gpuDetected: true, cudaRecipeAvailable: true, cudaValidated: true,
    cudaProbe: { ...passingCuda, ctranslate2_cuda_success: false } }).workerProfile, 'linux-x86_64-cpu');
});

test('readiness derives technical processing and release approval from current component evidence', () => {
  const components = Object.fromEntries(['managed_python', 'backend_environment', 'worker_environment', 'backend_service']
    .map((name) => [name, { state: 'verified', verification: name === 'backend_service' ? 'internal_health_identity' : 'trusted_recipe' }]));
  for (const name of ['ffmpeg', 'silero', 'whisper', 'pyannote', 'ecapa']) {
    components[name] = { state: 'runtime_ready', runtime_ready: true };
  }
  components.ffmpeg.redistribution_approved = true;
  let readiness = evaluateRuntimeReadiness(components);
  assert.deepEqual(readiness, { technical_ready: true, production_ready: true, blockers: [], release_blockers: [] });
  components.ffmpeg.redistribution_approved = false;
  readiness = evaluateRuntimeReadiness(components);
  assert.equal(readiness.technical_ready, true, 'FFmpeg redistribution approval is not an inference prerequisite');
  assert.equal(readiness.production_ready, false);
  assert.equal(readiness.release_blockers[0].code, 'FFMPEG_RELEASE_REVIEW_REQUIRED');
  components.ffmpeg.redistribution_approved = true;
  components.pyannote = { state: 'waiting_for_token', failure_code: 'HF_MODEL_ACCESS_REQUIRED', message: 'Accept access.' };
  readiness = evaluateRuntimeReadiness(components);
  assert.equal(readiness.technical_ready, false);
  assert.deepEqual(readiness.blockers[0], { component: 'pyannote', code: 'HF_MODEL_ACCESS_REQUIRED', message: 'Accept access.' });
  components.pyannote = { state: 'failed', failure_code: 'PYANNOTE_RUNTIME_VALIDATION_FAILED', message: 'Offline inference failed.' };
  readiness = evaluateRuntimeReadiness(components);
  assert.equal(readiness.blockers[0].code, 'PYANNOTE_RUNTIME_VALIDATION_FAILED');
  components.pyannote = { state: 'runtime_ready', runtime_ready: true };
  components.ffmpeg.redistribution_approved = false;
  components.backend_service = { state: 'failed', failure_code: 'BACKEND_EXITED', message: 'Backend exited.' };
  readiness = evaluateRuntimeReadiness(components);
  assert.equal(readiness.technical_ready, false);
  assert.equal(readiness.blockers.find((item) => item.component === 'backend_service').code, 'BACKEND_EXITED');
  assert.equal(readiness.release_blockers.find((item) => item.component === 'ffmpeg').code, 'FFMPEG_RELEASE_REVIEW_REQUIRED');
  assert.equal(readiness.technical_ready, false, 'backend failure is technical; FFmpeg compliance remains separately reported');
});

test('each technical prerequisite independently blocks readiness and repaired evidence clears stale errors', () => {
  const components = Object.fromEntries(['managed_python', 'backend_environment', 'worker_environment']
    .map((name) => [name, { state: 'verified', verification: 'trusted_recipe' }]));
  for (const name of ['ffmpeg', 'silero', 'whisper', 'pyannote', 'ecapa']) {
    components[name] = { state: 'runtime_ready', runtime_ready: true };
  }
  components.backend_service = { state: 'verified', verification: 'internal_health_identity' };
  const restore = structuredClone(components);
  for (const name of ['managed_python', 'backend_environment', 'worker_environment', 'backend_service',
    'ffmpeg', 'silero', 'whisper', 'pyannote', 'ecapa']) {
    const expected = structuredClone(restore);
    expected[name] = { state: 'failed', failure_code: `${name.toUpperCase()}_FAILED`, message: `${name} repair required.` };
    const failed = evaluateRuntimeReadiness(expected);
    assert.equal(failed.technical_ready, false, `${name} failure must block technical readiness`);
    assert.equal(failed.blockers[0].component, name);
    assert.equal(failed.blockers[0].message, `${name} repair required.`);
    const repaired = evaluateRuntimeReadiness(restore);
    assert.equal(repaired.technical_ready, true);
    assert.deepEqual(repaired.blockers, [], 'resolved failure text must not remain in the new evaluation');
  }
  const untrustedService = evaluateRuntimeReadiness({ ...restore,
    backend_service: { state: 'verified', verification: 'generic_healthz' } });
  assert.equal(untrustedService.technical_ready, false);
  assert.equal(untrustedService.blockers[0].component, 'backend_service');
});

test('checked-in backend recipe identity is verified and unsupported components remain blocked', () => {
  const root = path.resolve('..');
  assert.ok(assertTrustedBackendRecipe(root).lock.endsWith(path.join('backend', 'uv.lock')));
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cpu'].available, true);
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cpu'].validated, true);
  assert.equal(runtimeManifest.worker.profiles['linux-x86_64-cuda'].validated, true);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].available, true);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].validated, false);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].lock_verified, true);
  assert.equal(runtimeManifest.worker.profiles['macos-arm64'].inference_validated, false);
  assert.equal(initialComponents().pyannote.state, 'waiting_for_token');
  assert.equal(initialComponents().ecapa.state, 'pending');
  assert.equal(initialComponents().runtime_probes.state, 'pending');
  assert.equal(runtimeManifest.runtime_validation.available, true);
  for (const probe of runtimeManifest.runtime_validation.probes) {
    assert.match(probe.environment_role, /^(worker|backend|model_manager|worker_cuda_candidate)$/);
    assert.equal(sha(path.resolve(root, probe.helper)), probe.sha256, `trusted helper hash: ${probe.component}`);
  }
});

test('runtime executor reports an explicitly unavailable worker recipe without pretending it was installed', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-plan-'));
  const recipe = structuredClone(runtimeManifest);
  recipe.ffmpeg.available = false;
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
  const sourceProject = path.join(root, 'workers', 'dataset');
  const project = path.join(sourceProject, 'runtime-profiles', 'linux-cpu-test');
  fs.mkdirSync(project, { recursive: true });
  fs.mkdirSync(path.join(root, 'packages', 'model_manager'), { recursive: true });
  fs.writeFileSync(path.join(sourceProject, 'pyproject.toml'), '[project]\nname="source-fixture"\n');
  const lock = path.join(project, 'uv.lock'); const manifestFile = path.join(project, 'pyproject.toml');
  fs.writeFileSync(lock, 'fixture lock'); fs.writeFileSync(manifestFile, '[project]\nname="worker-fixture"\n');
  const sha = (file) => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.ffmpeg.available = false;
  runtimeRecipe.worker.source_manifest_sha256 = sha(path.join(sourceProject, 'pyproject.toml'));
  runtimeRecipe.worker.profiles['linux-x86_64-cpu'] = { available: true, validated: true,
    project: 'workers/dataset/runtime-profiles/linux-cpu-test', lockfile: 'uv.lock', lock_sha256: sha(lock), project_manifest_sha256: sha(manifestFile),
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
  tasks.paths = { uvExecutable: '/trusted/uv', pythonRoot: root, home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root,
    stagingRoot: path.join(root, 'staging', 'bootstrap'), quarantineRoot: path.join(root, 'quarantine') };
  const context = { attemptId: '00000000-0000-4000-8000-000000000099', managedPython: '/managed/python',
    signal: new AbortController().signal, componentStatus: tasks.initialBootstrapStatus(),
    onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_worker_environment', context);
  assert.equal(context.componentStatus.components.worker_environment.state, 'verified');
  assert.equal(context.componentStatus.components.worker_environment.verification, 'trusted_lock_and_import_probe');
  assert.equal(context.componentStatus.components.dataset_processing.state, 'blocked');
  fs.rmSync(root, { recursive: true, force: true });
});

test('an interrupted CUDA candidate environment is quarantined and reinstalled from its pinned lock', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-cuda-candidate-repair-'));
  const applicationRoot = path.join(root, 'application');
  fs.mkdirSync(path.join(applicationRoot, 'workers'), { recursive: true });
  fs.cpSync(path.resolve('..', 'workers/dataset'), path.join(applicationRoot, 'workers/dataset'), { recursive: true, dereference: true });
  fs.mkdirSync(path.join(applicationRoot, 'packages'), { recursive: true });
  fs.cpSync(path.resolve('..', 'packages/model_manager'), path.join(applicationRoot, 'packages/model_manager'), { recursive: true, dereference: true });
  for (const name of ['model-catalog.json', 'model-manifest.schema.json']) {
    const target = path.join(applicationRoot, 'workers/dataset', name);
    if (fs.lstatSync(target).isSymbolicLink()) {
      const source = fs.realpathSync(target);
      fs.unlinkSync(target);
      fs.copyFileSync(source, target);
    }
  }
  const recipe = runtimeManifest.worker.profiles['linux-x86_64-cuda'];
  const target = path.join(root, 'environments', 'worker',
    `v${recipe.environment_version}-${recipe.lock_sha256.slice(0, 16)}-linux-x86_64-cuda`);
  fs.mkdirSync(target, { recursive: true }); // crash left a directory, but no usable interpreter
  const libraries = path.join(target, 'lib/python3.12/site-packages/nvidia');
  let installs = 0;
  let processFailure = null;
  const calls = [];
  const report = { kind: 'SPEECHCRAFT_CUDA_ENV_PROBE', torch: '2.8.0', torchaudio: '2.8.0', cuda: '12.8',
    gpu: 'NVIDIA RTX fixture', ctranslate2: '4.7.2', ctranslate2_devices: 1,
    libraries: { cublas: 'libcublas.so.12', cudnn: 'libcudnn.so.9', cudart: 'libcudart.so.12' },
    torch_cuda_success: true };
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot, availableBytes: 32 * 1024 ** 3,
    processRunner: async (_executable, args, options) => {
      try {
      calls.push(args[0]);
      const code = args[2] || '';
      if (args[0] === 'sync') {
        installs += 1;
        const stagedProject = args[args.indexOf('--project') + 1];
        assert.equal(options.cwd, stagedProject);
        assert.notEqual(stagedProject, path.join(applicationRoot, recipe.project));
        assert.match(stagedProject, /staging\/bootstrap\/attempt-[^/]+\/cuda-candidate\/worker-source\/workers\/dataset\/runtime-profiles\/linux-cuda$/);
        assert.equal(fs.existsSync(path.join(stagedProject, 'pyproject.toml')), true);
        assert.equal(createHash('sha256').update(fs.readFileSync(path.join(stagedProject, 'pyproject.toml'))).digest('hex'), recipe.project_manifest_sha256);
        assert.equal(createHash('sha256').update(fs.readFileSync(path.join(stagedProject, recipe.lockfile))).digest('hex'), recipe.lock_sha256);
        fs.mkdirSync(path.join(target, 'bin'), { recursive: true });
        fs.writeFileSync(path.join(target, 'bin/python'), 'fixture python');
        for (const [pkg, filename] of [['cublas', 'libcublas.so.12'], ['cudnn', 'libcudnn.so.9'], ['cuda_runtime', 'libcudart.so.12']]) {
          const lib = path.join(libraries, pkg, 'lib'); fs.mkdirSync(lib, { recursive: true }); fs.writeFileSync(path.join(lib, filename), 'fixture');
        }
        return { code: 0, stdout: '', stderr: '' };
      }
      if (code.includes('SPEECHCRAFT_CUDA_PACKAGE_ENV_PROBE')) return { code: 1, stdout: '', stderr: '' };
      if (code.includes('pathlib')) return { code: 0, stdout: [path.join(libraries, 'cublas/lib'), path.join(libraries, 'cudnn/lib'), path.join(libraries, 'cuda_runtime/lib')].join(':'), stderr: '' };
      if (code.includes('SPEECHCRAFT_CUDA_ENV_PROBE')) {
        assert.match(options.env.LD_LIBRARY_PATH, /site-packages\/nvidia/);
        return { code: 0, stdout: JSON.stringify(report), stderr: '' };
      }
      throw new Error('unexpected CUDA candidate process');
      } catch (error) { processFailure = error.message; throw error; }
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvExecutable: '/trusted/uv', uvCache: root, pythonRoot: root,
    hfHome: root, hfHub: root, hfXet: root, temporary: root,
    stagingRoot: path.join(root, 'staging', 'bootstrap'),
    quarantineRoot: path.join(root, 'quarantine') };
  fs.mkdirSync(tasks.paths.quarantineRoot, { recursive: true });
  const context = { attemptId: '00000000-0000-4000-8000-000000000142', managedPython: '/managed/python',
    signal: new AbortController().signal, componentStatus: tasks.initialBootstrapStatus(), onProgress() {} };
  context.componentStatus.host_details = { cuda_candidate: true };
  await tasks.installCudaCandidate(context);
  assert.equal(installs, 1, JSON.stringify({ component: context.componentStatus.components.worker_cuda_candidate, calls }));
  assert.equal(context.componentStatus.components.worker_cuda_candidate.state, 'verified',
    `${processFailure || 'no mocked process assertion failed'}: ${JSON.stringify(context.componentStatus.components.worker_cuda_candidate)}`);
  assert.equal(fs.existsSync(target), true);
  assert.equal(fs.readdirSync(path.join(root, 'quarantine')).some((name) => name.includes('invalid-cuda-env')), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('restart recovery recognizes the previously validated CUDA environment without reinstalling it', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-cuda-recovery-'));
  const applicationRoot = path.resolve('..');
  const recipe = runtimeManifest.worker.profiles['linux-x86_64-cuda'];
  const target = path.join(root, 'environments', 'worker', `v${recipe.environment_version}-${recipe.lock_sha256.slice(0, 16)}-linux-x86_64-cuda`);
  const python = path.join(target, 'bin', 'python');
  fs.mkdirSync(path.dirname(python), { recursive: true }); fs.writeFileSync(python, 'fixture interpreter', { mode: 0o700 });
  const libraryRoot = path.join(target, 'lib/python3.12/site-packages/nvidia');
  const libraryPaths = [];
  for (const [packageName, filename] of [['cublas', 'libcublas.so.12'], ['cudnn', 'libcudnn.so.9'], ['cuda_runtime', 'libcudart.so.12']]) {
    const directory = path.join(libraryRoot, packageName, 'lib');
    fs.mkdirSync(directory, { recursive: true }); fs.writeFileSync(path.join(directory, filename), 'validated-library-fixture');
    libraryPaths.push(directory);
  }
  let installCommands = 0;
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot,
    processRunner: async (_executable, args) => {
      assert.equal(args[0], '-I');
      const program = args[2];
      if (program.includes('SPEECHCRAFT_WORKER_ENV_PROBE')) return { code: 0,
        stdout: JSON.stringify({ kind: 'SPEECHCRAFT_WORKER_ENV_PROBE', profile: 'linux-x86_64-cuda', missing: [] }), stderr: '' };
      if (program.includes('pathlib')) return { code: 0, stdout: libraryPaths.join(':'), stderr: '' };
      installCommands += 1;
      throw new Error('profile recovery must not run an installation command');
    } });
  tasks.environment = { profile: 'linux-x86_64', os: 'linux', architecture: 'x86_64', wsl: false };
  tasks.paths = { home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const recovered = await tasks.verifyExistingWorkerProfile('linux-x86_64-cuda', {
    attemptId: 'fixture', signal: new AbortController().signal, onChildProcess: () => true,
  });
  assert.equal(recovered.profile, 'linux-x86_64-cuda');
  assert.equal(recovered.target, target);
  assert.deepEqual(recovered.libraryPaths, libraryPaths);
  assert.equal(installCommands, 0, 'ordinary restart only verifies; it does not run uv sync');
  fs.rmSync(root, { recursive: true, force: true });
});

test('startup and launcher restore requests share one restore and a healthy result is reused', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-restore-once-'));
  const tasks = new RuntimeTasks({ dataDirectory: root });
  const state = { job: { attempt_id: 'runtime-attempt-1', bootstrap_status: { components: {
    backend_service: { state: 'verified', verification: 'internal_health_identity' },
  } } } };
  tasks.store = { snapshot: () => structuredClone(state) };
  tasks.service = { child: { exitCode: null, signalCode: null } };
  let executions = 0;
  tasks.performManagedServiceRestore = async () => { executions += 1; await new Promise((resolve) => setTimeout(resolve, 5)); return { restored: true }; };
  const [first, second] = await Promise.all([tasks.restoreManagedServices(), tasks.restoreManagedServices()]);
  assert.equal(first.restored, true); assert.equal(second.restored, true);
  assert.equal((await tasks.restoreManagedServices()).restored, true);
  assert.equal(executions, 1, 'the explicit launcher request reuses the startup restoration');
  tasks.service.child.exitCode = 1;
  await tasks.restoreManagedServices();
  assert.equal(executions, 2, 'an exited backend invalidates the cached healthy restoration');
  fs.rmSync(root, { recursive: true, force: true });
});

test('backend failure transitions invalidate stale instance health and readiness', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-stale-health-'));
  const state = { service_process: { pid: 44, nonce: 'owned' }, job: { bootstrap_status: { components: {
    managed_python: { state: 'verified' }, backend_environment: { state: 'verified' }, worker_environment: { state: 'verified' },
    ffmpeg: { state: 'runtime_ready' }, silero: { state: 'runtime_ready' }, whisper: { state: 'runtime_ready' },
    pyannote: { state: 'runtime_ready' }, ecapa: { state: 'runtime_ready' },
    backend_service: { state: 'verified', verification: 'internal_health_identity', health_instance_id: 'old-instance' },
  } } } };
  const store = { snapshot: () => structuredClone(state), commit(mutator) { mutator(state); } };
  const tasks = new RuntimeTasks({ dataDirectory: directory, store });
  const record = { pid: 44, nonce: 'owned' };
  tasks.updateServiceStatus({ state: 'restarting', failure_code: 'BACKEND_EXITED' }, record);
  assert.equal(state.job.bootstrap_status.components.backend_service.state, 'restarting');
  assert.equal(state.job.bootstrap_status.components.backend_service.verification, undefined);
  assert.equal(state.job.bootstrap_status.components.backend_service.health_instance_id, undefined);
  assert.equal(state.job.bootstrap_status.technical_processing_ready, false);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('transient restore failure is retried explicitly and a successful restore remains single-flight', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-restore-retry-'));
  const tasks = new RuntimeTasks({ dataDirectory: directory });
  tasks.store = { snapshot: () => ({ job: { attempt_id: 'same-attempt' } }) };
  tasks.service = { child: { exitCode: 0, signalCode: null } };
  let calls = 0;
  tasks.performManagedServiceRestore = async () => {
    calls += 1;
    return calls === 1 ? { restored: false, reason: 'BACKEND_START_FAILED' } : { restored: true };
  };
  assert.equal((await tasks.restoreManagedServices()).restored, false);
  assert.equal((await tasks.restoreManagedServices()).restored, false, 'failure is retained until explicitly retried');
  assert.equal((await tasks.restoreManagedServices({ retry: true })).restored, true);
  tasks.service.child.exitCode = null;
  tasks.store.snapshot = () => ({ job: { attempt_id: 'same-attempt', bootstrap_status: { components: {
    backend_service: { state: 'verified', verification: 'internal_health_identity' },
  } } } });
  assert.equal((await tasks.restoreManagedServices()).restored, true);
  assert.equal(calls, 2);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('shutdown aborts and joins in-flight restoration before stopping the backend', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-restore-shutdown-'));
  const tasks = new RuntimeTasks({ dataDirectory: directory });
  tasks.store = { snapshot: () => ({ job: { attempt_id: 'restore-attempt' } }) };
  let observedAbort = false;
  let backendStopped = false;
  tasks.service = { stop: async () => { backendStopped = true; } };
  tasks.performManagedServiceRestore = (signal) => new Promise((resolve) => {
    signal.addEventListener('abort', () => { observedAbort = true; resolve({ restored: false, reason: 'controller_shutting_down' }); }, { once: true });
  });
  const restoration = tasks.restoreManagedServices();
  await tasks.shutdown();
  assert.equal((await restoration).reason, 'controller_shutting_down');
  assert.equal(observedAbort, true);
  assert.equal(backendStopped, true);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('runtime probe runner requires a trusted helper and explicit completed inference result', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-probe-'));
  const helper = path.join(root, 'probe.py'); fs.writeFileSync(helper, '# trusted fixture');
  const sha = createHash('sha256').update(fs.readFileSync(helper)).digest('hex');
  const runtimeRecipe = structuredClone(runtimeManifest);
  runtimeRecipe.ffmpeg.available = false;
  runtimeRecipe.runtime_validation = { available: true, probes: [{ component: 'silero', helper: 'probe.py', sha256: sha,
    timeout_ms: 2000, arguments: [] }] };
  const probeAudio = path.join(root, 'fixture.wav'); fs.writeFileSync(probeAudio, 'fixture');
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    runtimeProbeAudio: probeAudio,
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

test('a passing CUDA Whisper probe updates the selected runtime device and worker profile', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-cuda-runtime-selection-'));
  const applicationRoot = path.resolve('..');
  const helper = path.join(applicationRoot, 'workers/dataset/scripts/runtime_probe.py');
  const fixture = path.join(applicationRoot, 'workers/dataset/runtime-fixtures/speech.wav');
  const recipe = structuredClone(runtimeManifest);
  recipe.runtime_validation.probes = [{ component: 'cuda', helper: 'workers/dataset/scripts/runtime_probe.py',
    sha256: sha(helper), environment_role: 'worker_cuda_candidate', fixture: 'speech', arguments: ['cuda'], timeout_ms: 600000 }];
  const cudaPath = path.join(root, 'environments/worker/cuda');
  const cpuPath = path.join(root, 'environments/worker/cpu');
  const report = { schema_version: 1, component: 'cuda', success: true, inference_completed: true,
    identity: 'cuda:NVIDIA RTX 4060', backend: 'torch:2.8.0/ctranslate2:4.7.2', device: 'NVIDIA RTX 4060',
    gpu_detected: true, driver_identification: '610.57.04', actual_execution_device: 'NVIDIA RTX 4060',
    torch_cuda_success: true, ctranslate2_cuda_success: true,
    required_libraries: { cublas: true, cudnn: true, cudart: true },
    measurements: { whisper_segments: 1, transcript_characters: 12 } };
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot, runtimeRecipe: recipe,
    processRunner: async (_python, _args, options) => {
      assert.equal(options.env.SPEECHCRAFT_PROBE_DEVICE, 'cuda');
      return { code: 0, stdout: JSON.stringify(report), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvCache: root, hfHome: root, hfHub: root, hfXet: root, temporary: root };
  const context = { attemptId: '00000000-0000-4000-8000-000000000141', signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(), onProgress() {},
    onComponentStatus(value) { context.componentStatus = value; } };
  context.componentStatus.components.worker_cuda_candidate = { state: 'verified', path: cudaPath, library_paths: [path.join(cudaPath, 'lib')] };
  context.componentStatus.components.worker_environment = { state: 'verified', path: cpuPath,
    effective_profile: 'linux-x86_64-cpu' };
  context.componentStatus.components.whisper = { state: 'runtime_ready', identity: 'whisper-large-v3-sha256:fixture',
    device: 'cpu', verification: 'trusted_inference_probe' };
  context.componentStatus.host_details = { nvidia_gpus: ['NVIDIA RTX 4060'] };
  await tasks.validateRuntime(context);
  assert.equal(context.componentStatus.runtime_profile.accelerator, 'cuda');
  assert.equal(context.componentStatus.components.worker_environment.effective_profile, 'linux-x86_64-cuda');
  assert.equal(context.componentStatus.components.whisper.device, 'NVIDIA RTX 4060');
  assert.equal(context.componentStatus.components.whisper.verification, 'trusted_cpu_and_cuda_inference_probes');
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
  runtimeRecipe.ffmpeg.available = false;
  runtimeRecipe.runtime_validation = { available: true, probes: [
    { component: 'ffmpeg', helper: 'failed.py', sha256: sha(failed), environment_role: 'worker', arguments: ['ffmpeg'], timeout_ms: 2000 },
    { component: 'silero', helper: 'good.py', sha256: sha(good), environment_role: 'worker', arguments: ['silero'], timeout_ms: 2000 },
  ] };
  const calls = [];
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    runtimeProbeAudio: path.join(root, 'fixture.wav'),
    processRunner: async (executable) => {
      calls.push(executable);
      return calls.length === 1
        ? { code: 1, stdout: JSON.stringify({ schema_version: 1, component: 'ffmpeg', success: false, inference_completed: false, error_code: 'runtime_tool_unavailable', error: 'Managed FFmpeg is absent.' }), stderr: '' }
        : { code: 0, stdout: JSON.stringify({ schema_version: 1, component: 'silero', success: true,
          inference_completed: true, identity: 'silero-fixture', device: 'cpu' }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  fs.writeFileSync(path.join(root, 'fixture.wav'), 'fixture');
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
      const action = args.findIndex((value) => value === 'install' || value === 'status');
      const role = args[action + 1];
      return { code: 0, stdout: JSON.stringify({ status: 'verified', install_ready: true, integrity_verified: true,
        inventory_exact: role === 'diarization' ? true : null,
        root: path.join(root, 'models', role), role, files: [{ path: 'tiny-fixture', status: 'verified' }] }), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: true, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvCache: root, hfHome: path.join(root, 'hf'), hfHub: path.join(root, 'hf', 'hub'),
    hfXet: path.join(root, 'hf', 'xet'), temporary: path.join(root, 'tmp'), pythonRoot: root };
  tasks.getManagerPython = () => '/managed/model-manager/python';
  const context = { attemptId: '00000000-0000-4000-8000-000000000101', signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(), onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_models', context);
  assert.deepEqual(calls.map((args) => args[args.findIndex((value) => value === 'install' || value === 'status') + 1]),
    ['speech_detection', 'asr', 'speaker_purity', 'diarization']);
  assert.ok(calls.every((args) => args.includes('--platform') && args[args.indexOf('--platform') + 1] === 'windows-wsl2'));
  assert.equal(context.componentStatus.components.silero.state, 'files_verified');
  assert.equal(context.componentStatus.components.whisper.state, 'files_verified');
  assert.equal(context.componentStatus.components.pyannote.state, 'files_verified');
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});

test('gated controller acquisition uses only the trusted role and sends token through stdin', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-gated-model-stage-'));
  const token = `hf_${'fixtureSecretMarker'.repeat(2)}`;
  const calls = [];
  const tasks = new RuntimeTasks({ dataDirectory: root, availableBytes: 32 * 1024 ** 3,
    processRunner: async (python, args, options) => {
      calls.push({ python, args, env: options.env, stdinData: Buffer.isBuffer(options.stdinData) ? Buffer.from(options.stdinData) : options.stdinData });
      const actionAt = args.findIndex((value) => ['install', 'status', 'authorize-install'].includes(value));
      const action = args[actionAt];
      const role = args[actionAt + 1];
      if (action === 'status' && role === 'diarization') return { code: 1,
        stdout: JSON.stringify({ status: 'missing', install_ready: false }), stderr: '' };
      const report = { status: 'verified', install_ready: true, integrity_verified: true,
        inventory_exact: role === 'diarization' ? true : null, root: path.join(root, 'models', role), role };
      return { code: 0, stdout: JSON.stringify(report), stderr: '' };
    } });
  tasks.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  tasks.paths = { home: root, uvCache: root, hfHome: path.join(root, 'hf'), hfHub: path.join(root, 'hf', 'hub'),
    hfXet: path.join(root, 'hf', 'xet'), temporary: root, pythonRoot: root };
  tasks.getManagerPython = () => '/trusted/model-manager/bin/python';
  const componentStatus = tasks.initialBootstrapStatus();
  const context = { attemptId: '00000000-0000-4000-8000-000000000102', authorizationToken: token,
    signal: new AbortController().signal, componentStatus, bootstrapStatus: componentStatus,
    onComponentStatus(value) { this.componentStatus = value; }, onProgress() {} };
  await tasks.run('install_models', context);
  const gated = calls.find((call) => call.args.includes('authorize-install'));
  assert.ok(gated);
  assert.equal(gated.args[gated.args.indexOf('authorize-install') + 1], 'diarization');
  assert.equal(gated.args.includes(token), false);
  assert.equal(JSON.stringify(gated.env).includes(token), false);
  assert.equal(gated.stdinData.toString('utf8'), `${token}\n`);
  gated.stdinData.fill(0);
  assert.equal(context.componentStatus.components.pyannote.state, 'files_verified');
  assert.equal(context.componentStatus.components.pyannote.runtime_ready, false);
  assert.equal(context.componentStatus.production_ready, false);
  fs.rmSync(root, { recursive: true, force: true });
});
