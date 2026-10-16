#!/usr/bin/env node
// Run a staged standalone Next.js frontend with the bundled Node binary and
// controlled local controller/backend fixtures. This intentionally does not
// place the source frontend node_modules on Node's resolution path.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const args = process.argv.slice(2);
const value = (name, fallback) => { const index = args.indexOf(name); return index < 0 ? fallback : args[index + 1]; };
const frontend = path.resolve(value('--frontend', ''));
const node = path.resolve(value('--node', ''));
if (!value('--frontend', null) || !value('--node', null)) {
  throw new Error('Usage: standalone-smoke.mjs --frontend STAGED_FRONTEND --node BUNDLED_NODE');
}
for (const file of [path.join(frontend, 'server.js'), node, path.join(root, 'setup-controller/src/server.js')]) {
  if (!fs.statSync(file).isFile()) throw new Error(`Required smoke-test file is missing: ${file}`);
}
if (frontend.startsWith(path.join(root, 'frontend'))) throw new Error('The staged frontend must be outside the source frontend directory.');

const dataDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-standalone-smoke-'));
fs.chmodSync(dataDirectory, 0o700);
const children = [];
const url = 'http://127.0.0.1:3002';
let fakeBackend;
function start(command, childArgs, cwd, env) {
  const child = spawn(command, childArgs, { cwd, env, shell: false, stdio: ['ignore', 'ignore', 'pipe'] });
  let diagnostic = '';
  child.stderr.setEncoding('utf8').on('data', (chunk) => { diagnostic = (diagnostic + chunk).slice(-3000); });
  children.push({ child, diagnostic: () => diagnostic });
  return child;
}
async function waitFor(address) {
  for (let count = 0; count < 120; count += 1) {
    try { return await fetch(address, { cache: 'no-store', signal: AbortSignal.timeout(1000) }); } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${address}.`);
}
async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  await Promise.race([once(child, 'exit'), new Promise((_, reject) => setTimeout(() => reject(new Error('Fixture process did not stop.')), 5000))]);
}

try {
  fakeBackend = http.createServer(async (request, response) => {
    if (request.url === '/health') { response.setHeader('content-type', 'application/json'); response.end('{"status":"ok"}'); return; }
    if (request.url === '/audio') { response.statusCode = 206; response.setHeader('accept-ranges', 'bytes'); response.setHeader('content-range', 'bytes 0-3/8'); response.end('RIFF'); return; }
    if (request.url === '/upload') { let bytes = 0; for await (const chunk of request) bytes += chunk.length; response.setHeader('content-type', 'application/json'); response.end(JSON.stringify({ bytes })); return; }
    response.statusCode = 404; response.end();
  });
  await new Promise((resolve) => fakeBackend.listen(8010, '127.0.0.1', resolve));
  const controllerEnvironment = { ...process.env,
    SPEECHCRAFT_SETUP_ALLOWED_HOSTS: '127.0.0.1:4317,localhost:4317,127.0.0.1:3002,localhost:3002',
    SPEECHCRAFT_SETUP_ALLOWED_ORIGINS: 'http://127.0.0.1:4317,http://localhost:4317,http://127.0.0.1:3002,http://localhost:3002' };
  const controller = start(node, [path.join(root, 'setup-controller/src/server.js'), '--data-dir', dataDirectory, '--port', '4317', '--mode', 'simulated'], root, controllerEnvironment);
  await waitFor('http://127.0.0.1:4317/setup-api/health');
  const identity = Buffer.from('standalone-smoke-private-identity').toString('base64url').padEnd(43, 'A').slice(0, 43);
  const frontendEnvironment = { ...process.env, NODE_ENV: 'production', HOSTNAME: '127.0.0.1', PORT: '3002',
    SPEECHCRAFT_SETUP_SESSION_BRIDGE: '1', SPEECHCRAFT_SETUP_BRIDGE_LOOPBACK_CONFIRMED: '1',
    SPEECHCRAFT_SETUP_CONTROLLER_URL: 'http://127.0.0.1:4317', SPEECHCRAFT_SETUP_BOOTSTRAP_FILE: path.join(dataDirectory, 'bootstrap.token'),
    SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS: 'http://127.0.0.1:3002,http://localhost:3002',
    SPEECHCRAFT_LAUNCHER_INSTALL_ID: 'a'.repeat(64), SPEECHCRAFT_LAUNCHER_PRIVATE_IDENTITY: identity,
    SPEECHCRAFT_BACKEND_URL: 'http://127.0.0.1:8010', NODE_PATH: '' };
  const server = start(node, [path.join(frontend, 'server.js')], frontend, frontendEnvironment);
  const page = await waitFor(`${url}/setup`);
  assert.match(await page.text(), /Preparing SpeechCraft/);
  for (const route of ['/login', '/lab']) assert.equal((await fetch(`${url}${route}`)).status, 200);
  const sessionResponse = await fetch(`${url}/setup-api/session`, { method: 'POST', headers: { origin: url, 'content-type': 'application/json' }, body: '{}' });
  assert.equal(sessionResponse.status, 201);
  const cookie = sessionResponse.headers.get('set-cookie').split(';', 1)[0];
  const csrf = (await sessionResponse.json()).csrf_token;
  assert.equal((await fetch(`${url}/setup-api/status`, { headers: { cookie } })).status, 200);
  assert.equal((await fetch(`${url}/setup-api/start`, { method: 'POST', headers: { origin: url, cookie, 'content-type': 'application/json', 'x-speechcraft-csrf': csrf }, body: '{}' })).status, 202);
  const events = await fetch(`${url}/setup-api/events`, { headers: { cookie, accept: 'text/event-stream' } });
  assert.match(new TextDecoder().decode((await events.body.getReader().read()).value), /event: snapshot/);
  assert.equal((await fetch(`${url}/sc-api/health`)).status, 200);
  const range = await fetch(`${url}/sc-api/audio`, { headers: { range: 'bytes=0-3' } });
  assert.equal(range.status, 206); assert.equal(await range.text(), 'RIFF');
  const upload = await fetch(`${url}/sc-api/upload`, { method: 'POST', body: Buffer.alloc(11 * 1024 * 1024, 1) });
  assert.equal(upload.status, 200); assert.equal((await upload.json()).bytes, 11 * 1024 * 1024);
  console.log('Standalone smoke passed: rendered routes, setup session/CSRF/SSE, backend proxy, range response, and 11 MiB upload.');
} catch (error) {
  for (const entry of children) if (entry.child.exitCode !== null) error.message += `\n${entry.diagnostic()}`;
  throw error;
} finally {
  for (const entry of children.reverse()) await stop(entry.child).catch(() => {});
  await new Promise((resolve) => fakeBackend?.close(resolve));
  fs.rmSync(dataDirectory, { recursive: true, force: true });
}
