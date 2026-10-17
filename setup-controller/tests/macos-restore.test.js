import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { RuntimeTasks } from '../src/runtime-tasks.js';
import { runtimeManifest } from '../src/runtime-plan.js';

test('completed macOS restoration verifies the Apple Silicon worker and FFmpeg recipe', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-mac-restore-'));
  const recipe = runtimeManifest.worker.profiles['macos-arm64'];
  const target = path.join(root, 'environments', 'worker', `v${recipe.environment_version}-${recipe.lock_sha256.slice(0, 16)}-macos-arm64`);
  fs.mkdirSync(path.join(target, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(target, 'bin', 'python'), 'fixture', { mode: 0o755 });
  const profiles = [];
  const store = {
    snapshot: () => ({ onboarding: { completed: true }, job: { id: 'job', attempt_id: 'attempt', attempt_count: 1, state: 'completed', mode: 'runtime', plan_id: 'speechcraft-runtime-v1',
      bootstrap_status: { runtime_profile: { workerProfile: 'macos-arm64', profile: 'macos-arm64' }, components: { model_manager: { path: path.join(root, 'environments', 'manager') }, backend_service: { state: 'blocked' } } } } }),
    commit: () => {},
  };
  fs.mkdirSync(path.join(root, 'environments', 'manager'), { recursive: true });
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: path.resolve('..'), store,
    hostInspector: () => ({ os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false }),
    processRunner: async (_executable, args) => {
      const program = args[2] || '';
      if (program.includes('macos-arm64')) profiles.push('macos-arm64');
      if (program.includes('linux-x86_64-cpu')) profiles.push('linux-x86_64-cpu');
      if (program.includes('-version')) return { code: 0, stdout: 'ffmpeg version n8.1.3-18-g46d899d7a0\nconfiguration: --disable-gpl --disable-nonfree\n', stderr: '' };
      return { code: 0, stdout: JSON.stringify({ kind: 'SPEECHCRAFT_WORKER_ENV_PROBE', profile: 'macos-arm64', missing: [] }), stderr: '' };
    } });
  tasks.paths = { pythonRoot: root, environmentRoot: path.join(root, 'environments', 'manager') };
  const result = await tasks.performManagedServiceRestore(new AbortController().signal);
  assert.equal(profiles.includes('linux-x86_64-cpu'), false);
  assert.equal(profiles.includes('macos-arm64'), true);
  assert.notEqual(result.reason, 'linux_worker_selected');
  fs.rmSync(root, { recursive: true, force: true });
});
