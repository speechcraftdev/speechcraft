import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.resolve(frontend, '..');
const controllerEntry = path.join(root, 'setup-controller/src/server.js');
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-setup-ui-e2e-'));
async function availablePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const { port } = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}
const configuredController = new URL(process.env.SPEECHCRAFT_SETUP_CONTROLLER_URL || 'http://127.0.0.1:4317');
const controllerPort = Number(configuredController.port || 4317);
const appPort = await availablePort();
const controllerOrigin = `http://127.0.0.1:${controllerPort}`;
const appOrigin = `http://127.0.0.1:${appPort}`;
const localhostControllerOrigin = `http://localhost:${controllerPort}`;
const localhostAppOrigin = `http://localhost:${appPort}`;
const allowedHosts = `127.0.0.1:${controllerPort},localhost:${controllerPort},127.0.0.1:${appPort},localhost:${appPort}`;
const allowedOrigins = `${controllerOrigin},${localhostControllerOrigin},${appOrigin},${localhostAppOrigin}`;
const children = new Set();
if (!fs.existsSync(path.join(frontend, '.next/BUILD_ID'))) {
  throw new Error('Build the frontend with `npm run build` before running setup API integration tests.');
}

function launch(executable, args, cwd, env) {
  const child = spawn(executable, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });
  children.add(child);
  let output = '';
  child.stdout.setEncoding('utf8').on('data', (part) => { output += part; });
  child.stderr.setEncoding('utf8').on('data', (part) => { output += part; });
  return { child, output: () => output };
}

async function waitFor(url, predicate = (response) => response.ok, timeoutMs = 40_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { redirect: 'manual', cache: 'no-store' });
      if (predicate(response)) return response;
    } catch (error) { lastError = error; }
    await delay(250);
  }
  throw new Error(`Timed out waiting for ${url}: ${lastError?.message || 'service did not become ready'}`);
}

async function stop(handle) {
  if (!handle || handle.child.exitCode !== null || handle.child.signalCode !== null) return;
  const exited = once(handle.child, 'exit');
  handle.child.kill('SIGTERM');
  await Promise.race([exited, delay(8000).then(() => { throw new Error('Service did not stop in time.'); })]);
  children.delete(handle.child);
}

async function request(pathname, { method = 'GET', cookie, csrf, origin = appOrigin, target = appOrigin, body, headers = {} } = {}) {
  return fetch(`${target}${pathname}`, { method, cache: 'no-store', redirect: 'manual', headers: {
    host: new URL(target).host,
    ...(origin ? { origin } : {}),
    ...(cookie ? { cookie } : {}),
    ...(csrf ? { 'x-speechcraft-csrf': csrf } : {}),
    ...(body !== undefined ? { 'content-type': 'application/json' } : {}),
    ...headers,
  }, body: body === undefined ? undefined : JSON.stringify(body) });
}

const sseReaders = new WeakMap();
async function sseFrame(reader, timeoutMs = 8000) {
  let state = sseReaders.get(reader);
  if (!state) {
    state = { decoder: new TextDecoder(), buffered: '', ready: [] };
    sseReaders.set(reader, state);
  }
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (state.ready.length) return state.ready.shift();
    const next = await Promise.race([reader.read(), delay(Math.max(1, deadline - Date.now())).then(() => { throw new Error('Timed out waiting for setup SSE.'); })]);
    if (next.done) throw new Error('Setup SSE closed before a frame arrived.');
    state.buffered += state.decoder.decode(next.value, { stream: true }).replaceAll('\r\n', '\n');
    const frames = state.buffered.split('\n\n');
    state.buffered = frames.pop() || '';
    state.ready.push(...frames);
    if (state.ready.length) return state.ready.shift();
  }
  throw new Error('Timed out waiting for setup SSE.');
}

function createSseFrameParser() {
  let buffer = '';
  const ready = [];
  return (chunk) => {
    buffer += chunk.replaceAll('\r\n', '\n');
    const frames = buffer.split('\n\n');
    buffer = frames.pop() || '';
    ready.push(...frames);
    return ready.splice(0);
  };
}
const frameParser = createSseFrameParser();
assert.deepEqual(frameParser('id: 1\nevent: first\ndata: {}\n\nid: 2\nevent: sec'),
  ['id: 1\nevent: first\ndata: {}']);
assert.deepEqual(frameParser('ond\ndata: {}\n\n'), ['id: 2\nevent: second\ndata: {}'],
  'the parser retains a split frame and emits it when the remaining chunk arrives');
assert.deepEqual(frameParser('id: 3\nevent: third\ndata: {}\n\nid: 4\nevent: fourth\ndata: {}\n\n'),
  ['id: 3\nevent: third\ndata: {}', 'id: 4\nevent: fourth\ndata: {}'], 'one chunk may contain multiple complete frames');
const queuedParser = createSseFrameParser();
assert.deepEqual(queuedParser('id: 5\ndata: a\n\nid: 6\ndata: b\n\n'),
  ['id: 5\ndata: a', 'id: 6\ndata: b'], 'multiple event frames from a single transport chunk are retained in order');

try {
  const commonEnv = { ...process.env, SPEECHCRAFT_SETUP_ALLOWED_HOSTS: allowedHosts,
    SPEECHCRAFT_SETUP_ALLOWED_ORIGINS: allowedOrigins };
  let controller = launch(process.execPath, [controllerEntry, '--data-dir', dataDir, '--port', String(controllerPort), '--step-delay-ms', '100'], root, commonEnv);
  const listening = await waitFor(`${controllerOrigin}/setup-api/health`);
  assert.equal((await listening.json()).status, 'ok');
  const bootstrapFile = path.join(dataDir, 'bootstrap.token');
  const nextEnv = { ...process.env, NODE_ENV: 'production',
    SPEECHCRAFT_SETUP_SESSION_BRIDGE: '1',
    SPEECHCRAFT_SETUP_BRIDGE_LOOPBACK_CONFIRMED: '1',
    SPEECHCRAFT_SETUP_CONTROLLER_URL: controllerOrigin,
    SPEECHCRAFT_SETUP_BOOTSTRAP_FILE: bootstrapFile,
    SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS: `${appOrigin},${localhostAppOrigin}` };
  const next = launch(process.execPath, [path.join(frontend, 'node_modules/next/dist/bin/next'), 'start', '--hostname', '127.0.0.1', '-p', String(appPort)], frontend, nextEnv);
  await waitFor(`${appOrigin}/login`);

  const initial = await request('/setup-api/session');
  assert.equal(initial.status, 401, 'the page begins without an established controller session');
  assert.equal((await request('/setup-api/session?token=not-accepted', { method: 'POST', body: {} })).status, 400,
    'the bridge never accepts credentials or options in a URL');
  const rejected = await request('/setup-api/session', { method: 'POST', origin: 'https://attacker.invalid', body: {} });
  assert.equal(rejected.status, 403, 'the bridge rejects an untrusted browser origin');
  const badBody = await request('/setup-api/session', { method: 'POST', body: { path: '/etc/passwd' } });
  assert.equal(badBody.status, 400, 'the bridge accepts no client-supplied configuration');
  assert.equal((await request('/setup-api/session', { method: 'POST', body: { extra: 'x'.repeat(200) } })).status, 413,
    'the bridge rejects oversized request bodies');

  const hiddenBootstrap = fs.readFileSync(bootstrapFile, 'utf8').trim();
  const missingCredential = `${bootstrapFile}.held`;
  fs.renameSync(bootstrapFile, missingCredential);
  const missingFile = await request('/setup-api/session', { method: 'POST', body: {} });
  assert.equal(missingFile.status, 503, 'a missing private bootstrap file fails safely');
  assert.equal((await missingFile.text()).includes(hiddenBootstrap), false);
  fs.renameSync(missingCredential, bootstrapFile);
  fs.chmodSync(bootstrapFile, 0o644);
  const unsafeFile = await request('/setup-api/session', { method: 'POST', body: {} });
  assert.equal(unsafeFile.status, 503, 'a group/world-readable bootstrap file is rejected');
  fs.chmodSync(bootstrapFile, 0o600);

  const established = await request('/setup-api/session', { method: 'POST', body: {} });
  assert.equal(established.status, 201);
  assert.equal(established.headers.get('cache-control'), 'no-store');
  const setCookie = established.headers.get('set-cookie');
  assert.match(setCookie, /HttpOnly/i);
  assert.match(setCookie, /Path=\/setup-api/i);
  assert.match(setCookie, /SameSite=Strict/i);
  assert.doesNotMatch(setCookie, /Domain=/i, 'cookie is host-only at the Next.js origin');
  const session = await established.json();
  assert.equal(typeof session.csrf_token, 'string');
  assert.equal(JSON.stringify(session).includes(hiddenBootstrap), false);
  const cookie = setCookie.split(';', 1)[0];

  const statusResponse = await request('/setup-api/status', { cookie });
  const statusText = await statusResponse.text();
  assert.equal(statusResponse.status, 200, `same-origin rewrite reaches the authenticated controller: ${statusText}`);
  assert.equal(statusText.includes(hiddenBootstrap), false);
  assert.equal(JSON.parse(statusText).mode, 'simulated');
  const modelsResponse = await request('/setup-api/models', { cookie });
  assert.equal(modelsResponse.status, 200);
  const modelsText = await modelsResponse.text();
  assert.equal(modelsText.includes(hiddenBootstrap), false);
  assert.equal(JSON.parse(modelsText).models.length, 4);
  assert.equal((await request('/setup-api/status')).status, 401, 'the proxied status endpoint requires a session');
  assert.equal((await request('/setup-api/start', { method: 'POST', cookie, body: {} })).status, 403,
    'the proxied controller rejects missing CSRF protection');
  assert.equal((await request('/setup-api/start', { method: 'POST', cookie, csrf: session.csrf_token,
    origin: 'https://attacker.invalid', body: {} })).status, 403, 'the proxied controller rejects an untrusted mutation origin');

  const streamResponse = await request('/setup-api/events', { cookie, headers: { accept: 'text/event-stream' } });
  assert.equal(streamResponse.status, 200);
  const reader = streamResponse.body.getReader();
  const firstFrame = await sseFrame(reader);
  assert.match(firstFrame, /event: snapshot/);
  assert.equal(firstFrame.includes(hiddenBootstrap), false);
  const start = await request('/setup-api/start', { method: 'POST', cookie, csrf: session.csrf_token, body: {} });
  assert.equal(start.status, 202, 'authenticated same-origin mutation succeeds');
  const startText = await start.text();
  assert.equal(startText.includes(hiddenBootstrap), false);
  const updateFrame = await sseFrame(reader);
  assert.match(updateFrame, /event: (job_started|stage_started|task_progress)/);
  assert.equal(updateFrame.includes(hiddenBootstrap), false);
  await reader.cancel();

  const refreshed = await request('/setup-api/session', { cookie });
  assert.equal(refreshed.status, 200, 'refresh can recover CSRF in memory using the HTTP-only cookie');
  const refreshedBody = await refreshed.json();
  const latestCookie = cookie;
  let latestStatus;
  let latestBody;
  const deadline = Date.now() + 10_000;
  do {
    latestStatus = await request('/setup-api/status', { cookie: latestCookie });
    latestBody = await latestStatus.json();
    if (latestBody.job.state === 'completed') break;
    await delay(100);
  } while (Date.now() < deadline);
  assert.equal(latestStatus.status, 200);
  assert.equal(latestBody.job.state, 'completed');
  assert.equal(typeof refreshedBody.csrf_token, 'string');

  const repair = await request('/setup-api/repair', { method: 'POST', cookie, csrf: refreshedBody.csrf_token, body: {} });
  assert.equal(repair.status, 202, 'repair starts another controller-owned setup pass');
  const repairJob = (await repair.json()).job;
  assert.equal((await request('/setup-api/cancel', { method: 'POST', cookie, csrf: refreshedBody.csrf_token, body: {} })).status, 202,
    'cancellation is accepted through the same authenticated API');
  const stateUntil = async (expected) => {
    const until = Date.now() + 8000;
    while (Date.now() < until) {
      const response = await request('/setup-api/status', { cookie });
      const snapshot = await response.json();
      if (snapshot.job.state === expected) return snapshot;
      await delay(50);
    }
    throw new Error(`Setup job did not reach ${expected}.`);
  };
  const cancelled = await stateUntil('cancelled');
  assert.equal(cancelled.production_ready, false);
  assert.equal((await request('/setup-api/retry', { method: 'POST', cookie, csrf: refreshedBody.csrf_token, body: {} })).status, 202,
    'explicit retry reuses the same durable setup job');
  const retried = await stateUntil('completed');
  assert.equal(retried.job.id, repairJob.id);
  assert.equal(retried.production_ready, false, 'simulated completion never implies production readiness');

  await stop(controller);
  const unavailable = await request('/setup-api/session', { method: 'POST', body: {} });
  assert.equal(unavailable.status, 503,
    'the session bridge fails safely when the controller is stopped');
  assert.equal((await unavailable.text()).includes(hiddenBootstrap), false);
  controller = launch(process.execPath, [controllerEntry, '--data-dir', dataDir, '--port', String(controllerPort), '--step-delay-ms', '100'], root, commonEnv);
  await waitFor(`${controllerOrigin}/setup-api/health`);
  const expired = await request('/setup-api/session', { cookie });
  assert.equal(expired.status, 401, 'controller restart invalidates the in-memory session');
  const reestablished = await request('/setup-api/session', { method: 'POST', body: {} });
  assert.equal(reestablished.status, 201, 'the bridge reads the rotated launcher credential after restart');
  const recoveredCookie = reestablished.headers.get('set-cookie').split(';', 1)[0];
  const recovered = await request('/setup-api/status', { cookie: recoveredCookie });
  assert.equal((await recovered.json()).job.state, 'completed', 'durable setup state survives controller restart');

  await waitFor(`${localhostAppOrigin}/login`);
  const localSession = await request('/setup-api/session', { method: 'POST', target: localhostAppOrigin,
    origin: localhostAppOrigin, body: {} });
  assert.equal(localSession.status, 201, 'the explicitly allowlisted localhost origin can establish its host-only session');
  const localCookie = localSession.headers.get('set-cookie').split(';', 1)[0];
  const localStatus = await request('/setup-api/status', { target: localhostAppOrigin,
    origin: localhostAppOrigin, cookie: localCookie });
  assert.equal(localStatus.status, 200);

  const secretText = `${controller.output()}\n${next.output()}`;
  assert.equal(secretText.includes(hiddenBootstrap), false, 'bootstrap secret is absent from service logs');
  console.log('PASS: same-origin setup API bridge, cookie/CSRF, authorization boundary, SSE, refresh and restart recovery');
} catch (error) {
  console.error(error);
  for (const child of children) child.kill('SIGTERM');
  process.exitCode = 1;
} finally {
  await Promise.all([...children].map(async (child) => {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGTERM');
      await Promise.race([once(child, 'exit'), delay(5000)]);
      if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
    }
  }));
  fs.rmSync(dataDir, { recursive: true, force: true });
}
