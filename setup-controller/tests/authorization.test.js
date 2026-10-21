import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { runOwnedProcess } from '../src/bootstrap.js';
import { createSetupController } from '../src/server.js';
import { initialComponents } from '../src/runtime-plan.js';
import { openEvents, temporaryController } from './helpers.js';

const active = new Set();
afterEach(async () => {
  for (const fixture of active) {
    try { await fixture.app.close(); } catch {}
    fs.rmSync(fixture.directory, { recursive: true, force: true });
  }
  active.clear();
});

function filesUnder(root) {
  const found = [];
  for (const name of fs.readdirSync(root, { withFileTypes: true })) {
    const target = path.join(root, name.name);
    if (name.isDirectory()) found.push(...filesUnder(target));
    else if (name.isFile()) found.push(target);
  }
  return found;
}

function gatedTestTasks({ entered, release } = {}) {
  let calls = 0;
  let invocation = null;
  const stagesRun = [];
  return {
    stages: ['install_worker_environment', 'install_models', 'validate_runtime'],
    taskId: () => 'runtime:install_models',
    initialBootstrapStatus: () => ({ components: initialComponents(), production_ready: false }),
    initialProgress: (_stage, message) => ({ phase: 'queued', current: null, total: null, percent: null, message }),
    completionProgress: () => ({ phase: 'complete', current: null, total: null, percent: null, message: 'plan completed' }),
    get calls() { return calls; },
    get invocation() { return invocation; },
    get stagesRun() { return stagesRun; },
    async run(stage, context) {
      calls += 1;
      stagesRun.push(stage);
      if (stage === 'install_worker_environment') return;
      if (stage === 'validate_runtime') {
        assert.equal(context.authorizationToken, null, 'later setup stages receive no authorization credential');
        assert.ok(invocation.tokenBuffer.every((byte) => byte === 0), 'the controller buffer is cleared before the next stage');
        return;
      }
      if (!context.authorizationToken) {
        const status = structuredClone(context.bootstrapStatus);
        status.components.pyannote = { ...status.components.pyannote, state: 'waiting_for_token', runtime_ready: false };
        context.onComponentStatus(status);
        context.waitForUser(status, 'Community-1 needs authorization.');
        return;
      }
      entered?.();
      if (release) await release;
      const token = context.authorizationToken;
      const script = "let v='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>v+=x);process.stdin.on('end',()=>process.stdout.write(JSON.stringify({bytes:Buffer.byteLength(v),newline:v.endsWith('\\n')})))";
      const args = ['-e', script];
      const env = { PATH: process.env.PATH || '' };
      let output = '';
      invocation = { args, env, tokenBuffer: context.authorizationToken };
      const child = await runOwnedProcess(process.execPath, args, { cwd: os.tmpdir(), env,
        stdinData: `${token}\n`, signal: context.signal,
        onChildProcess: (record, phase) => context.onChildProcess(
          record && !record.clear ? { ...record, attempt_id: context.attemptId } : record, phase),
        onOutput: (_stream, chunk) => { output += chunk.toString('utf8'); },
      });
      const report = JSON.parse(child.stdout);
      assert.deepEqual(report, { bytes: Buffer.byteLength(`${token}\n`), newline: true });
      assert.equal(output.includes(token), false);
      const status = structuredClone(context.bootstrapStatus);
      status.components.pyannote = { state: 'files_verified', identity: 'test-only-child-fixture', runtime_ready: false };
      context.onComponentStatus(status);
    },
  };
}

test('authorization API is session/CSRF/origin protected and only accepts one token field', async () => {
  const tasks = gatedTestTasks();
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  assert.equal((await fetch(`${f.base}/setup-api/models`)).status, 401);
  const tokenBody = { token: 'hf_' + 'a'.repeat(24) };
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: tokenBody, csrf: '' })).status, 403);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: tokenBody, originValue: 'https://attacker.invalid' })).status, 403);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token: 'bad' } })).status, 400);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { ...tokenBody, extra: 'x' } })).status, 400);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: '{"token":"hf_' + 'a'.repeat(24) + '","token":"hf_' + 'b'.repeat(24) + '"}' })).status, 400);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: {} })).status, 400);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: `{"token":"hf_${'a'.repeat(5000)}"}` })).status, 413);
  assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: '{' })).status, 400);
  assert.equal((await f.request('/setup-api/models/pyannote/authorize', { method: 'POST', body: tokenBody })).status, 400,
    'raw credentials cannot be submitted to an endpoint that starts setup');
  const models = await f.request('/setup-api/models').then((r) => r.json());
  assert.equal(models.models.find((item) => item.role === 'diarization').model_id, 'pyannote/speaker-diarization-community-1');
  await f.app.close();
});

test('managed-service restore is authenticated, idempotent, and does not create a setup attempt', async () => {
  const tasks = gatedTestTasks();
  let restoreCalls = 0;
  const restoreOptions = [];
  tasks.restoreManagedServices = (options = {}) => {
    restoreCalls += 1;
    restoreOptions.push(options);
    return Promise.resolve({ restored: true });
  };
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  f.app.store.commit((state) => {
    state.onboarding = { schema_version: 1, completed: true, installation_identity: state.installation_identity,
      completed_at: new Date().toISOString(), setup_plan: 'speechcraft-runtime-v1', profile: 'linux-x86_64-cpu',
      required_checks: ['managed_python'] };
  }, 'test_completed_installation');
  const denied = await fetch(`${f.base}/setup-api/services/restore`, { method: 'POST', headers: {
    origin: f.origin, 'content-type': 'application/json', 'x-speechcraft-csrf': f.auth.csrf_token,
  }, body: '{}' });
  assert.equal(denied.status, 401, 'a 401 is preserved as an authentication failure');
  const accepted = await f.request('/setup-api/services/restore', { method: 'POST', body: {} });
  assert.equal(accepted.status, 202);
  assert.deepEqual(await accepted.json().then(({ accepted: ok, state }) => ({ accepted: ok, state })), { accepted: true, state: 'restoring' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(restoreCalls, 1);
  assert.equal(f.app.store.snapshot().job, null, 'service restore does not create a new setup job');
  const retry = await f.request('/setup-api/services/restore/retry', { method: 'POST', body: {} });
  assert.equal(retry.status, 202);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(restoreOptions, [{ retry: false }, { retry: true }]);
});

test('launcher shutdown endpoint requires session/CSRF and closes only after acknowledging the request', async () => {
  const f = await temporaryController({ mode: 'simulated', tasks: gatedTestTasks() }); active.add(f);
  const denied = await fetch(`${f.base}/setup-api/shutdown`, { method: 'POST', headers: {
    origin: f.origin, 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(denied.status, 401);
  const accepted = await f.request('/setup-api/shutdown', { method: 'POST', body: {} });
  assert.equal(accepted.status, 202);
  assert.deepEqual(await accepted.json().then(({ accepted: ok, state }) => ({ accepted: ok, state })), { accepted: true, state: 'stopping' });
  const deadline = Date.now() + 2_000;
  let closed = false;
  while (Date.now() < deadline) {
    try { await fetch(`${f.base}/setup-api/health`, { signal: AbortSignal.timeout(100) }); }
    catch { closed = true; break; }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(closed, true, 'authenticated shutdown closes the controller after returning 202');
});

test('a waiting Community-1 authorization survives controller restart without retaining a credential', async () => {
  const f = await temporaryController({ mode: 'runtime', tasks: gatedTestTasks() }); active.add(f);
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  const before = f.app.store.snapshot().job;
  assert.equal(before.state, 'waiting_for_user');
  const originalFetch = globalThis.fetch;
  const previouslyVerifiedToken = 'hf_SECRET_MARKER_before_restart_' + 'a'.repeat(24);
  try {
    globalThis.fetch = async (input, init = {}) => String(input).startsWith('https://huggingface.co/')
      ? { ok: true, status: init.method === 'HEAD' ? 302 : 200 }
      : originalFetch(input, init);
    const verified = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token: previouslyVerifiedToken } });
    assert.equal(verified.status, 200);
    assert.equal(f.app.store.snapshot().job.state, 'waiting_for_user');
  } finally { globalThis.fetch = originalFetch; }
  await f.app.close();
  assert.equal(f.app.store.snapshot().job.state, 'waiting_for_user', 'graceful stop must preserve waiting state');

  const restartedTasks = gatedTestTasks();
  const restarted = await createSetupController({ dataDirectory: f.directory, port: 0,
    mode: 'runtime', tasks: restartedTasks });
  f.app = restarted;
  const restartedBase = `http://127.0.0.1:${restarted.port}`;
  const after = restarted.store.snapshot().job;
  assert.equal(after.id, before.id);
  assert.equal(after.state, 'waiting_for_user');
  assert.equal(after.bootstrap_status.components.pyannote.state, 'waiting_for_token');
  assert.equal(JSON.stringify(after).includes('hf_SECRET_MARKER_'), false);
  assert.equal(restarted.controller.pendingAuthorizationToken, null, 'restart discards the ephemeral verified credential');
  const bootstrap = fs.readFileSync(restarted.bootstrapPath, 'utf8').trim();
  const restartedOrigin = restartedBase;
  const sessionResponse = await fetch(`${restartedBase}/setup-api/session`, { method: 'POST', headers: {
    origin: restartedOrigin, authorization: `Bearer ${bootstrap}`, 'content-type': 'application/json',
  }, body: '{}' });
  assert.equal(sessionResponse.status, 201);
  const session = await sessionResponse.json();
  const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
  const request = (route, body) => fetch(`${restartedBase}${route}`, { method: 'POST', headers: {
    origin: restartedOrigin, cookie, 'x-speechcraft-csrf': session.csrf_token, 'content-type': 'application/json',
  }, body: JSON.stringify(body) });
  const noCredential = await request('/setup-api/start', {});
  assert.equal(noCredential.status, 200);
  assert.equal(restartedTasks.calls, 0, 'a stale success screen cannot silently resume setup after restart');
});

test('token waits ephemerally, is piped to an actual child, and never appears in persistent/public state', async () => {
  const tasks = gatedTestTasks();
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  const token = 'hf_SECRET_MARKER_' + 'x'.repeat(24);
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  let status = await f.request('/setup-api/status').then((r) => r.json());
  assert.equal(status.job.state, 'waiting_for_user');
  assert.equal(status.components.pyannote.state, 'waiting_for_token');
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (input, init = {}) => String(input).startsWith('https://huggingface.co/')
      ? { ok: true, status: init.method === 'HEAD' ? 302 : 200 }
      : originalFetch(input, init);
    const verified = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token } });
    assert.equal(verified.status, 200);
    assert.deepEqual(await verified.json(), { verified: true });
    assert.equal(f.app.store.snapshot().job.state, 'waiting_for_user', 'Verify does not resume or start setup');
    assert.equal(tasks.calls, 2, 'Verify does not install packages or download models');
  } finally { globalThis.fetch = originalFetch; }
  const response = await f.request('/setup-api/start', { method: 'POST', body: {} });
  assert.equal(response.status, 202);
  const result = await response.json();
  assert.equal(result.job.state, 'running');
  assert.equal(JSON.stringify(result).includes(token), false);
  await f.app.controller.waitForIdle();
  status = await f.request('/setup-api/status').then((r) => r.json());
  assert.equal(status.job.state, 'completed');
  assert.equal(status.production_ready, false);
  assert.equal(tasks.calls, 5);
  assert.deepEqual(tasks.stagesRun, [
    'install_worker_environment', 'install_models',
    'install_worker_environment', 'install_models', 'validate_runtime',
  ], 'authorization revalidates the managed worker environment before model and runtime checks');
  assert.equal(tasks.invocation.args.includes(token), false);
  assert.equal(JSON.stringify(tasks.invocation.env).includes(token), false);
  assert.ok(tasks.invocation.tokenBuffer.every((byte) => byte === 0), 'controller clears its mutable token buffer after install_models settles');
  const publicText = JSON.stringify({ status, diagnostics: await f.request('/setup-api/diagnostics').then((r) => r.json()), models: await f.request('/setup-api/models').then((r) => r.json()) });
  assert.equal(publicText.includes(token), false);
  for (const file of filesUnder(f.directory)) assert.equal(fs.readFileSync(file).includes(token), false, path.relative(f.directory, file));
});

test('preauthorization verifies token and gated repository access before setup starts', async () => {
  const tasks = gatedTestTasks();
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  const token = 'hf_SECRET_MARKER_' + 'y'.repeat(24);
  const originalFetch = globalThis.fetch;
  const checks = [];
  try {
    globalThis.fetch = async (input, init = {}) => {
      const url = String(input);
      if (!url.startsWith('https://huggingface.co/')) return originalFetch(input, init);
      checks.push({ url, method: init.method || 'GET', authorization: init.headers?.authorization });
      return { ok: true, status: init.method === 'HEAD' ? 302 : 200 };
    };
    const response = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token } });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { verified: true });
    assert.equal(checks.length, 2);
    assert.ok(checks.every((check) => check.authorization === `Bearer ${token}`));
    assert.equal(checks[0].method, 'GET');
    assert.equal(checks[1].method, 'HEAD');
    assert.equal(checks[1].url.includes('3533c8cf8e369892e6b79ff1bf80f7b0286a54ee'), true);
    assert.equal(JSON.stringify(f.app.store.snapshot()).includes(token), false);
    assert.equal(f.app.controller.pendingAuthorizationToken.toString('utf8'), token);
    assert.equal(f.app.store.snapshot().job, null, 'Verify creates no setup job');
    assert.equal(tasks.calls, 0, 'Verify does not install packages or download models');

    await f.request('/setup-api/start', { method: 'POST', body: {} });
    await f.app.controller.waitForIdle();
    assert.equal(tasks.invocation.tokenBuffer.every((byte) => byte === 0), true);
    assert.equal(JSON.stringify(f.app.store.snapshot()).includes(token), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('invalid token, unaccepted model conditions, and network failures are actionable without starting setup', async () => {
  const tasks = gatedTestTasks();
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (input, init = {}) => {
      if (!String(input).startsWith('https://huggingface.co/')) return originalFetch(input, init);
      if (String(input).includes('whoami')) return new Response('', { status: 401 });
      return new Response('', { status: init.method === 'HEAD' ? 403 : 200 });
    };
    let response = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token: 'hf_' + 'i'.repeat(24) } });
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, 'HF_TOKEN_INVALID');

    globalThis.fetch = async (input, init = {}) => {
      if (!String(input).startsWith('https://huggingface.co/')) return originalFetch(input, init);
      return new Response('', { status: init.method === 'HEAD' ? 403 : 200 });
    };
    response = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token: 'hf_' + 'c'.repeat(24) } });
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, 'HF_MODEL_ACCESS_REQUIRED');

    globalThis.fetch = async (input, init = {}) => {
      if (!String(input).startsWith('https://huggingface.co/')) return originalFetch(input, init);
      throw new TypeError('network down');
    };
    response = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token: 'hf_' + 'n'.repeat(24) } });
    assert.equal(response.status, 503);
    assert.equal((await response.json()).error.code, 'HF_NETWORK_ERROR');
    assert.equal(f.app.store.snapshot().job, null);
    assert.equal(tasks.calls, 0, 'verification failures never start package installation or model acquisition');
  } finally { globalThis.fetch = originalFetch; }
});

test('a verified credential can be consumed only by the verifying setup session', async () => {
  const tasks = gatedTestTasks();
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async (input, init = {}) => String(input).startsWith('https://huggingface.co/')
      ? { ok: true, status: init.method === 'HEAD' ? 302 : 200 }
      : originalFetch(input, init);
    const token = 'hf_SECRET_MARKER_session_' + 's'.repeat(24);
    assert.equal((await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token } })).status, 200);
    const bootstrap = fs.readFileSync(f.app.bootstrapPath, 'utf8').trim();
    const otherSession = await fetch(`${f.base}/setup-api/session`, { method: 'POST', headers: {
      origin: f.origin, authorization: `Bearer ${bootstrap}`, 'content-type': 'application/json',
    }, body: '{}' });
    const other = await otherSession.json();
    const otherCookie = otherSession.headers.get('set-cookie').split(';', 1)[0];
    const stolenStart = await fetch(`${f.base}/setup-api/start`, { method: 'POST', headers: {
      origin: f.origin, cookie: otherCookie, 'x-speechcraft-csrf': other.csrf_token, 'content-type': 'application/json',
    }, body: '{}' });
    assert.equal(stolenStart.status, 202);
    await f.app.controller.waitForIdle();
    assert.equal(f.app.store.snapshot().job.state, 'waiting_for_user');
    assert.equal(tasks.invocation, null, 'the second session cannot consume or start with the verified token');
    assert.equal(f.app.controller.pendingAuthorizationToken.toString('utf8'), token);
    const ownerStart = await f.request('/setup-api/start', { method: 'POST', body: {} });
    assert.equal(ownerStart.status, 202);
    await f.app.controller.waitForIdle();
    assert.equal(f.app.store.snapshot().job.state, 'completed');
  } finally { globalThis.fetch = originalFetch; }
});

test('duplicate Download Models requests resume a waiting job exactly once', async () => {
  let entered;
  let release;
  const inChild = new Promise((resolve) => { entered = resolve; });
  const wait = new Promise((resolve) => { release = resolve; });
  const tasks = gatedTestTasks({ entered, release: wait });
  const f = await temporaryController({ mode: 'runtime', tasks }); active.add(f);
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  const token = 'hf_' + 'z'.repeat(30);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init = {}) => String(input).startsWith('https://huggingface.co/')
    ? { ok: true, status: init.method === 'HEAD' ? 302 : 200 }
    : originalFetch(input, init);
  try {
    const verified = await f.request('/setup-api/models/pyannote/preauthorize', { method: 'POST', body: { token } });
    assert.equal(verified.status, 200);
    const first = await f.request('/setup-api/start', { method: 'POST', body: {} });
    assert.equal(first.status, 202);
    await inChild;
    const second = await f.request('/setup-api/start', { method: 'POST', body: {} });
    assert.equal(second.status, 200);
    assert.equal((await second.json()).idempotent, true);
    release();
    await f.app.controller.waitForIdle();
  } finally { globalThis.fetch = originalFetch; }
});

test('all status, mutation, diagnostics, model, and SSE responses sanitize private job fields', async () => {
  const f = await temporaryController({ mode: 'runtime', tasks: gatedTestTasks() }); active.add(f);
  await f.request('/setup-api/start', { method: 'POST', body: {} });
  await f.app.controller.waitForIdle();
  const marker = 'hf_SECRET_MARKER_public_serializer_123456';
  f.app.store.commit((state) => {
    state.job.authorizationToken = marker;
    state.job.private_nonce = 'private-process-nonce';
    state.job.child_process = { pid: 123, identity: 'private-process-identity', nonce: 'private-owner-nonce' };
    state.job.bootstrap_status.private_exception = { stack: marker };
    state.job.bootstrap_status.components.private_process_identity = { state: 'private', identity: marker };
    state.job.bootstrap_status.components.runtime_probes.components = ['whisper', { identity: marker }];
  }, 'test_private_job_fields');
  const eventClient = await openEvents(f.base, f.cookie, { origin: f.origin });
  const snapshot = await eventClient.nextEvent();
  eventClient.close();
  const bodies = [];
  for (const route of ['/setup-api/status', '/setup-api/models', '/setup-api/diagnostics']) {
    bodies.push(await f.request(route).then((r) => r.json()));
  }
  bodies.push(await (await f.request('/setup-api/start', { method: 'POST', body: {} })).json());
  f.app.store.commit((state) => { state.job.child_process = null; }, 'test_remove_fake_owner');
  bodies.push(await (await f.request('/setup-api/cancel', { method: 'POST', body: {} })).json());
  await f.app.controller.waitForIdle();
  bodies.push(await (await f.request('/setup-api/retry', { method: 'POST', body: {} })).json());
  await f.app.controller.waitForIdle();
  bodies.push(await (await f.request('/setup-api/repair', { method: 'POST', body: {} })).json());
  await f.app.controller.waitForIdle();
  bodies.push(await (await f.request('/setup-api/cancel', { method: 'POST', body: {} })).json());
  await f.app.controller.waitForIdle();
  bodies.push(await (await f.request('/setup-api/retry', { method: 'POST', body: {} })).json());
  await f.app.controller.waitForIdle();
  const authToken = 'hf_' + 'serializerSecret'.repeat(2);
  assert.equal((await f.request('/setup-api/models/pyannote/authorize', { method: 'POST', body: { token: authToken } })).status, 400);
  const serialized = JSON.stringify({ snapshot: snapshot.data, bodies });
  for (const secret of [marker, authToken, 'private-process-nonce', 'private-process-identity', 'private-owner-nonce']) {
    assert.equal(serialized.includes(secret), false, `public API leaked ${secret}`);
  }
  assert.equal(Object.hasOwn(bodies[0].components, 'private_process_identity'), false);
  assert.equal(snapshot.data.job.id, f.app.store.snapshot().job.id);
});
