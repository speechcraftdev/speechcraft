import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import { afterEach, test } from 'node:test';
import { assertTransition, serializePublicJob } from '../src/controller.js';
import { PRODUCTION_STAGES, PRODUCTION_STAGE_TITLES } from '../src/runtime-plan.js';
import { StateStore, StateStoreError } from '../src/state-store.js';
import { openEvents, readUntilEvent, temporaryController } from './helpers.js';

const active = new Set();

test('public setup status exposes Pyannote CUDA selection and a bounded validation reason', () => {
  const value = serializePublicJob({ bootstrap_status: { components: { pyannote: { state: 'runtime_ready', device: 'cpu',
    cuda_validation: { validated: false, device: 'cpu', failure_code: 'cuda_runtime_unavailable',
      reason: 'CUDA out of memory in Community-1 probe', measurements: { secret: 'never present' } } },
  }, runtime_profile: { pyannote_cuda_validated: false, pyannote_device: 'cpu' } } });
  assert.equal(value.bootstrap_status.runtime_profile.pyannote_cuda_validated, false);
  assert.equal(value.bootstrap_status.runtime_profile.pyannote_device, 'cpu');
  assert.match(value.bootstrap_status.components.pyannote.cuda_validation.reason, /out of memory/);
  assert.deepEqual(value.bootstrap_status.components.pyannote.cuda_validation.measurements, {});
});

test('public production stage indicator matches the controller stage source of truth', () => {
  for (const [index, stage] of PRODUCTION_STAGES.entries()) {
    const job = serializePublicJob({ mode: 'runtime', stage });
    assert.equal(job.stage_index, index + 1);
    assert.equal(job.stage_count, 11);
    assert.equal(job.stage_title, PRODUCTION_STAGE_TITLES[stage]);
  }
  assert.equal(serializePublicJob({ mode: 'runtime', stage: 'not_a_production_stage' }).stage_index, undefined);
  assert.equal(serializePublicJob({ mode: 'runtime', stage: 'verify_services' }).stage_index, 11);
});

test('runtime controller advances the displayed step only after each production stage resolves', async () => {
  let waiting = null;
  const queued = [];
  const nextStage = () => queued.length ? Promise.resolve(queued.shift()) : new Promise((resolve) => { waiting = resolve; });
  const tasks = {
    stages: PRODUCTION_STAGES,
    taskId: (stage) => `runtime:${stage}`,
    initialProgress: (_stage, message) => ({ operation: 'checking', progress_mode: 'indeterminate', message }),
    initialBootstrapStatus: () => ({ components: {}, production_ready: false }),
    async run(stage) {
      let release;
      const current = { stage, done: new Promise((resolve) => { release = resolve; }), release: () => release() };
      if (waiting) { const resolve = waiting; waiting = null; resolve(current); } else queued.push(current);
      await current.done;
    },
  };
  const f = await fixture({ mode: 'runtime', tasks });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  for (let index = 0; index < PRODUCTION_STAGES.length; index += 1) {
    const current = await nextStage();
    const status = await f.request('/setup-api/status').then((response) => response.json());
    assert.equal(status.job.stage, current.stage);
    assert.equal(status.job.stage_index, index + 1);
    assert.equal(status.job.stage_count, 11);
    assert.ok(Date.parse(status.job.stage_started_at));
    current.release();
  }
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.stage_index, 11,
    'completion retains the actual final stage and never synthesizes a twelfth step');
});

afterEach(async () => {
  for (const fixture of active) {
    try { await fixture.app.close(); } catch {}
    fs.rmSync(fixture.directory, { recursive: true, force: true });
  }
  active.clear();
});

async function fixture(options) {
  const value = await temporaryController(options);
  active.add(value);
  return value;
}

test('state transitions reject illegal jumps and completed jobs can be rerun explicitly', () => {
  assertTransition('idle', 'running');
  assertTransition('interrupted', 'running');
  assertTransition('completed', 'running');
  assert.throws(() => assertTransition('running', 'idle'), { code: 'INVALID_STATE_TRANSITION' });
});

test('health is narrow and setup status requires authenticated session', async () => {
  const f = await fixture();
  assert.deepEqual(await (await fetch(`${f.base}/setup-api/health`)).json(), {
    status: 'ok', controller_status: 'running',
  });
  assert.equal((await fetch(`${f.base}/setup-api/status`)).status, 401);
  const status = await f.request('/setup-api/status');
  assert.equal(status.status, 200);
  const body = await status.json();
  assert.equal(body.job.state, 'idle');
  assert.equal(body.job.schema_version, 2);
  assert.equal(body.production_ready, false);
  const refreshed = await f.request('/setup-api/session');
  assert.equal(refreshed.status, 200);
  assert.equal((await refreshed.json()).csrf_token, f.auth.csrf_token);
  assert.match(f.setCookie, /HttpOnly/);
  assert.match(f.setCookie, /SameSite=Strict/);
});

test('runtime plan exposes component readiness and repair only reruns the trusted plan', async () => {
  const components = { ffmpeg: { state: 'blocked', verification: 'pending', failure_code: 'runtime_tool_unavailable', message: 'No trusted tool artifact.' } };
  const f = await fixture({ mode: 'runtime', tasks: {
    stages: ['inspect_system'], taskId: (stage) => `runtime:${stage}`,
    initialBootstrapStatus: () => ({ components, production_ready: false, bootstrap_ready: false }),
    initialProgress: (_stage, message) => ({ phase: 'queued', current: null, total: null, percent: null, message }),
    async run(_stage, context) { context.onProgress({ phase: 'verified', current: null, total: null, percent: null, message: 'fixture complete' }); },
    completionProgress: () => ({ phase: 'complete', current: null, total: null, percent: null, message: 'partial setup complete' }),
  } });
  const started = await f.request('/setup-api/start', { method: 'POST', body: {} }).then((r) => r.json());
  await f.app.controller.waitForIdle();
  let status = await f.request('/setup-api/status').then((r) => r.json());
  assert.equal(status.setup_plan, 'speechcraft-runtime-v1');
  assert.equal(status.mode, 'runtime');
  assert.equal(status.production_ready, false);
  assert.equal(status.onboarding.completed, false, 'a completed but incomplete runtime plan is not first-run completion');
  assert.equal(status.components.ffmpeg.state, 'blocked');
  const repaired = await f.request('/setup-api/repair', { method: 'POST', body: {} }).then((r) => r.json());
  assert.notEqual(repaired.job.id, started.job.id);
  await f.app.controller.waitForIdle();
  status = await f.request('/setup-api/status').then((r) => r.json());
  assert.equal(status.production_ready, false);
});

test('model telemetry is available from current status, model rows, and a fresh SSE snapshot without secrets', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ simulated: { stepDelayMs: 0, stageGate: async () => { entered(); await hold; } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  f.app.store.commit((state) => {
    state.job.bootstrap_status = { components: { whisper: { state: 'downloading', runtime_ready: false,
      message: 'Downloading Whisper Large-v3.', progress: { operation: 'downloading', artifact_type: 'model',
        artifact_name: 'Systran/faster-whisper-large-v3', progress_mode: 'bytes', role: 'asr',
        current_filename: '../../model.bin', transferred_bytes: 1_500_000, total_bytes: 3_000_000,
        percent: 50, bytes_per_second: 500_000, eta_seconds: 3, completed_files: 2,
        total_files: 5, transfer_indeterminate: false, stalled: false, sampled_at: new Date().toISOString(),
        signed_url: 'https://cdn.invalid/?token=secret', authorization: 'hf_do_not_publish' } } },
      production_ready: false };
  }, 'fixture_model_progress');
  const status = await f.request('/setup-api/status').then((response) => response.json());
  assert.equal(status.components.whisper.progress.transferred_bytes, 1_500_000);
  assert.equal(status.components.whisper.progress.current_filename, 'model.bin');
  const models = await f.request('/setup-api/models').then((response) => response.json());
  const whisper = models.models.find((item) => item.role === 'asr');
  assert.equal(whisper.progress.completed_files, 2);
  assert.equal(whisper.progress.total_bytes, 3_000_000);
  assert.doesNotMatch(JSON.stringify({ status, models }), /cdn\.invalid|secret|hf_do_not_publish|authorization|signed_url/i);

  const stream = await openEvents(f.base, f.cookie, { origin: f.origin });
  const snapshot = await stream.nextEvent();
  assert.equal(snapshot.event, 'snapshot');
  const data = snapshot.data;
  assert.equal(data.components.whisper.progress.bytes_per_second, 500_000);
  assert.equal(data.components.whisper.progress.eta_seconds, 3);
  assert.doesNotMatch(JSON.stringify(snapshot.data), /cdn\.invalid|secret|hf_do_not_publish|authorization|signed_url/i);
  stream.close();
  release();
  await f.app.controller.waitForIdle();
});

test('two concurrent starts return the same active job and only start once', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ simulated: { stepDelayMs: 0, stageGate: async (stage) => {
    if (stage === 'inspect_system') { entered(); await hold; }
  } } });
  const responses = await Promise.all([
    f.request('/setup-api/start', { method: 'POST', body: {} }),
    f.request('/setup-api/start', { method: 'POST', body: {} }),
  ]);
  assert.deepEqual(responses.map((response) => response.status).sort(), [200, 202]);
  const results = await Promise.all(responses.map((response) => response.json()));
  assert.equal(results[0].job.id, results[1].job.id);
  assert.equal(f.app.store.snapshot().events.filter((event) => event.type === 'job_started').length, 1);
  await atStage;
  release();
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'completed');
  assert.equal(f.app.controller.status().onboarding.completed, false, 'simulation never persists first-run completion');
});

test('browser disconnect, reconnect, replay and multiple clients do not own the job', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ simulated: { stepDelayMs: 0, stageGate: async (stage) => {
    if (stage === 'bootstrap_runtime') { entered(); await hold; }
  } } });
  const first = await openEvents(f.base, f.cookie, { origin: f.origin });
  const firstSnapshot = await first.nextEvent();
  assert.equal(firstSnapshot.event, 'snapshot');
  const second = await openEvents(f.base, f.cookie, { origin: f.origin });
  assert.equal((await second.nextEvent()).event, 'snapshot');
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  first.close();
  const reconnect = await openEvents(f.base, f.cookie, {
    origin: f.origin, lastEventId: firstSnapshot.id,
  });
  const events = [];
  for (let i = 0; i < 20; i += 1) {
    const event = await reconnect.nextEvent();
    events.push(event);
    if (event.event === 'snapshot') break;
  }
  const latestSnapshot = events.at(-1);
  assert.equal(latestSnapshot.event, 'snapshot');
  assert.equal(latestSnapshot.data.job.state, 'running');
  assert.ok(events.some((event) => event.event === 'stage_started'));
  release();
  const completedOnSecond = await readUntilEvent(second,
    (event) => event.data?.job?.state === 'completed');
  assert.equal(completedOnSecond.event, 'job_completed');
  await f.app.controller.waitForIdle();
  second.close();
  reconnect.close();
});

test('simulated stage failure is durable and explicit retry completes without readiness claim', async () => {
  let failOnce = true;
  const f = await fixture({ simulated: { stepDelayMs: 0,
    failureInjector: (stage, attempt) => {
      if (stage === 'install_dependencies' && attempt === 1 && failOnce) {
        failOnce = false;
        return true;
      }
      return false;
    } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  let status = await f.request('/setup-api/status').then((response) => response.json());
  assert.equal(status.job.state, 'failed');
  assert.equal(status.job.error.code, 'SIMULATED_STAGE_FAILURE');
  assert.equal(status.production_ready, false);
  const retry = await f.request('/setup-api/retry', { method: 'POST', body: {} });
  assert.equal(retry.status, 202);
  await f.app.controller.waitForIdle();
  status = await f.request('/setup-api/status').then((response) => response.json());
  assert.equal(status.job.state, 'completed');
  assert.equal(status.job.attempt_count, 2);
  assert.equal(status.production_ready, false);
  assert.equal(status.simulated, true);
});

test('cancellation remains nonterminal until the executor acknowledges stop', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ tasks: { async run(stage) {
    if (stage === 'inspect_system') { entered(); await hold; }
  } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  const cancelled = await f.request('/setup-api/cancel', { method: 'POST', body: {} });
  assert.equal(cancelled.status, 202);
  assert.equal((await cancelled.json()).job.state, 'cancelling');
  assert.throws(() => f.app.controller.retry(), { code: 'ATTEMPT_STILL_STOPPING' });
  release();
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'cancelled');
});

test('retry cannot overlap cancellation; late callbacks from old attempts cannot mutate the new attempt', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const callbacks = [];
  const f = await fixture({ tasks: { async run(stage, { onProgress }) {
    callbacks.push(onProgress);
    onProgress({ current: 1, total: 2, percent: 50, message: 'working' });
    if (callbacks.length === 1) { entered(); await hold; }
  } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  await f.request('/setup-api/cancel', { method: 'POST', body: {} });
  assert.throws(() => f.app.controller.retry(), { code: 'ATTEMPT_STILL_STOPPING' });
  const cancelAgain = f.app.controller.cancel();
  assert.equal(cancelAgain.idempotent, true);
  release();
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'cancelled');
  const retry = f.app.controller.retry();
  const newAttempt = retry.job.attempt_id;
  const revision = f.app.store.snapshot().revision;
  callbacks[0]({ current: 2, total: 2, percent: 100, message: 'obsolete callback' });
  assert.equal(f.app.store.snapshot().revision, revision);
  assert.notEqual(newAttempt, f.app.store.snapshot().events.find((event) => event.type === 'job_started').data.job.attempt_id);
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'completed');
});

test('multiple retries serialize and failure can be retried immediately', async () => {
  let calls = 0;
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ tasks: { async run() {
    calls += 1;
    if (calls === 1) throw Object.assign(new Error('fail'), { code: 'SIMULATED_STAGE_FAILURE' });
    if (calls === 2) { entered(); await hold; }
  } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'failed');
  const retries = await Promise.all([
    f.request('/setup-api/retry', { method: 'POST', body: {} }),
    f.request('/setup-api/retry', { method: 'POST', body: {} }),
  ]);
  assert.deepEqual(retries.map((response) => response.status).sort(), [202, 409]);
  await atStage;
  release();
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'completed');
});

test('uncertain subprocess ownership is durable and blocks retry until reconciliation', async () => {
  let runCount = 0;
  const f = await fixture({ mode: 'bootstrap', tasks: {
    stages: ['install_python'], taskId: (stage) => `bootstrap:${stage}`,
    initialProgress: (_stage, message) => ({ phase: 'queued', current: null, total: null, percent: null, message }),
    async run(_stage, { attemptId, onChildProcess }) {
      runCount += 1;
      if (runCount === 1) {
        onChildProcess({ pid: 999999, process_group_id: 999999, identity: 'fixture', nonce: 'fixture',
          attempt_id: attemptId, termination_status: 'termination_uncertain',
          diagnostic: 'termination could not be confirmed' });
        throw Object.assign(new Error('unsafe stop'), { code: 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN' });
      }
    },
  } });
  f.app.controller.start();
  await f.app.controller.waitForIdle();
  const failed = f.app.store.snapshot().job;
  assert.equal(failed.state, 'interrupted');
  assert.equal(failed.termination_status, 'termination_uncertain');
  assert.equal(failed.child_process.termination_status, 'termination_uncertain');
  assert.throws(() => f.app.controller.retry(), { code: 'PROCESS_RECONCILIATION_REQUIRED' });

  // Test the post-reconciliation path: the owner record may only be cleared by
  // the reconciler after it has established that the process group is gone.
  f.app.store.commit((state) => {
    state.job.child_process = null;
    state.job.termination_status = 'termination_confirmed';
  }, 'test_reconciliation_confirmed');
  f.app.controller.retry();
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'completed');
});

test('a simulated controller cannot retry a persisted real bootstrap job', async () => {
  const f = await fixture();
  f.app.store.commit((state) => {
    state.job = {
      schema_version: 2, id: '00000000-0000-4000-8000-000000000001',
      attempt_id: '00000000-0000-4000-8000-000000000002', state: 'interrupted',
      stage: 'install_python', task_id: 'bootstrap:install_python', mode: 'bootstrap',
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      started_at: new Date().toISOString(), completed_at: new Date().toISOString(),
      attempt_count: 1, revision: 0, progress: null, bootstrap_status: null,
      error: { code: 'CONTROLLER_RESTARTED', message: 'interrupted' },
    };
  }, 'test_interrupted_bootstrap');
  assert.throws(() => f.app.controller.retry(), { code: 'EXECUTOR_MODE_MISMATCH' });
});

test('cancellation terminal state follows executor acknowledgement and shutdown waits for it', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  let release;
  const hold = new Promise((resolve) => { release = resolve; });
  const f = await fixture({ tasks: { async run(stage) {
    if (stage === 'inspect_system') { entered(); await hold; }
  } } });
  f.app.controller.start();
  await atStage;
  const stopping = f.app.close();
  assert.equal(f.app.controller.status().job.state, 'cancelling');
  release();
  await stopping;
  assert.equal(f.app.controller.status().job.state, 'cancelled');
  const reopened = StateStore.open(f.directory);
  assert.equal(reopened.snapshot().job.state, 'cancelled');
  reopened.releaseOwnership();
});

test('graceful cancellation stays observable and idempotent after executor stops', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  const f = await fixture({ simulated: { stepDelayMs: 0, stageGate: async (stage) => {
    if (stage === 'inspect_system') { entered(); await new Promise(() => {}); }
  } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  const cancelled = await f.request('/setup-api/cancel', { method: 'POST', body: {} });
  assert.equal(cancelled.status, 202);
  await f.app.controller.waitForIdle();
  assert.equal(f.app.controller.status().job.state, 'cancelled');
  const again = await f.request('/setup-api/cancel', { method: 'POST', body: {} });
  assert.equal(again.status, 200);
  assert.equal((await again.json()).idempotent, true);
  assert.equal(f.app.store.snapshot().events.filter((event) => event.type === 'job_started').length, 1);
});

test('graceful controller shutdown persists cancellation and releases ownership', async () => {
  let entered;
  const atStage = new Promise((resolve) => { entered = resolve; });
  const f = await fixture({ simulated: { stepDelayMs: 0, stageGate: async (stage) => {
    if (stage === 'inspect_system') { entered(); await new Promise(() => {}); }
  } } });
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await atStage;
  await f.app.close();
  assert.equal(f.app.store.snapshot().job.state, 'cancelled');
  assert.equal(f.app.store.snapshot().job.error.code, 'CONTROLLER_SHUTDOWN');
  const reopened = StateStore.open(f.directory);
  assert.equal(reopened.snapshot().job.state, 'cancelled');
  reopened.releaseOwnership();
});

test('CSRF, origin, host, methods, query credentials, and caller execution fields are rejected', async () => {
  const f = await fixture();
  assert.equal((await f.request('/setup-api/start', { method: 'POST', body: {}, csrf: 'bad' })).status, 403);
  assert.equal((await f.request('/setup-api/start', { method: 'POST', body: {}, originValue: 'http://evil.invalid' })).status, 403);
  assert.equal((await f.request('/setup-api/status?token=secret')).status, 400);
  const badHost = await new Promise((resolve, reject) => {
    const request = http.request({ hostname: '127.0.0.1', port: f.app.port,
      path: '/setup-api/status', headers: { host: 'evil.invalid', cookie: f.cookie } }, (response) => resolve(response));
    request.once('error', reject);
    request.end();
  });
  assert.equal(badHost.statusCode, 400);
  assert.equal((await f.request('/setup-api/start', { method: 'PUT' })).status, 405);
  assert.equal((await f.request('/setup-api/start', { method: 'POST', body: '{' })).status, 400);
  const tooLarge = await f.request('/setup-api/start', {
    method: 'POST', body: JSON.stringify({ value: 'x'.repeat(17 * 1024) }),
  });
  assert.equal(tooLarge.status, 413);
  const injected = await f.request('/setup-api/start', {
    method: 'POST', body: { command: 'touch /tmp/should-not-exist', path: '/tmp' },
  });
  assert.equal(injected.status, 400);
  assert.equal(f.app.store.snapshot().job, null);
  assert.equal(injected.headers.get('access-control-allow-origin'), null);
});

test('secure cookie mode is explicit for a trusted HTTPS reverse proxy', async () => {
  const f = await fixture({ secureCookies: true });
  assert.match(f.setCookie, /; Secure/);
});

test('SSE older than retained history receives only an authoritative current snapshot', async () => {
  const f = await fixture();
  for (let index = 0; index < 270; index += 1) {
    f.app.store.commit(() => {}, 'fixture_progress');
  }
  const stream = await openEvents(f.base, f.cookie, { origin: f.origin, lastEventId: 0 });
  const snapshot = await stream.nextEvent();
  assert.equal(snapshot.event, 'snapshot');
  assert.equal(snapshot.id, f.app.store.snapshot().revision);
  assert.equal(snapshot.data.state_revision, snapshot.id);
  assert.equal(snapshot.data.production_ready, false);
  stream.close();
});

test('corrupt persistent state is rejected without silently replacing the file', async () => {
  const f = await fixture();
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  await f.app.close();
  active.delete(f);
  const stateFile = `${f.directory}/state.json`;
  fs.writeFileSync(stateFile, '{invalid', { mode: 0o600 });
  assert.throws(() => StateStore.open(f.directory), (error) =>
    error instanceof StateStoreError && error.code === 'STATE_CORRUPT');
  assert.equal(fs.readFileSync(stateFile, 'utf8'), '{invalid');
  fs.writeFileSync(stateFile, JSON.stringify({ schema_version: 99, revision: 0, job: null, events: [] }), { mode: 0o600 });
  assert.throws(() => StateStore.open(f.directory), (error) =>
    error instanceof StateStoreError && error.code === 'STATE_INVALID');
});
