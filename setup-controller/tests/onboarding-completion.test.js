import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { SetupController } from '../src/controller.js';
import { RuntimeTasks } from '../src/runtime-tasks.js';
import { StateStore } from '../src/state-store.js';

class VerifiedFixtureTasks extends RuntimeTasks {
  constructor(options) {
    super(options);
    this.stages = ['verify_services'];
    this.service = { verify: async () => ({ port: 8010 }), stop: async () => {} };
  }

  initialBootstrapStatus() {
    const status = super.initialBootstrapStatus();
    status.profile = 'linux-x86_64';
    status.runtime_profile = { profile: 'linux-x86_64', workerProfile: 'linux-x86_64-cpu', accelerator: 'cpu' };
    status.uv_verified = true;
    status.python_verified = true;
    status.model_manager_verified = true;
    for (const name of ['managed_python', 'backend_environment', 'worker_environment']) {
      status.components[name] = { state: 'verified', verification: 'test_fixture_identity', identity: `${name}:fixture` };
    }
    for (const name of ['ffmpeg', 'silero', 'whisper', 'pyannote', 'titanet']) {
      status.components[name] = { state: 'runtime_ready', runtime_ready: true,
        verification: 'controlled_verified_runtime_fixture', identity: `${name}:fixture` };
    }
    status.components.titanet.release_approved = false;
    status.components.titanet.message = 'TitaNet provenance remains unresolved in this internal acceptance fixture.';
    status.components.ffmpeg.redistribution_approved = false;
    return status;
  }
}

test('the production controller transition writes onboarding from evaluated runtime evidence, without seeding the record', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-onboarding-transition-'));
  const store = StateStore.open(directory);
  const tasks = new VerifiedFixtureTasks({ dataDirectory: store.directory, store });
  const controller = new SetupController(store, { tasks, mode: 'runtime' });
  try {
    assert.equal(store.snapshot().onboarding, null);
    controller.start();
    await controller.waitForIdle();
    const persisted = store.snapshot();
    assert.equal(persisted.job.state, 'completed');
    assert.equal(persisted.job.bootstrap_status.technical_processing_ready, true);
    assert.equal(persisted.onboarding.completed, true);
    assert.equal(persisted.onboarding.profile, 'linux-x86_64-cpu');
    assert.ok(persisted.job.bootstrap_status.release_blockers.some((item) => item.component === 'titanet'));
    assert.equal(persisted.job.bootstrap_status.production_ready, false);
  } finally {
    await controller.shutdown();
    store.releaseOwnership();
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
