import assert from 'node:assert/strict';
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
  await tasks.blockWorkerProfile(context);
  assert.equal(context.componentStatus.components.worker_environment.state, 'blocked');
  assert.equal(context.componentStatus.components.worker_environment.failure_code, 'runtime_profile_unavailable');
  assert.equal(context.componentStatus.components.dataset_processing.state, 'blocked');
  fs.rmSync(root, { recursive: true, force: true });
});
