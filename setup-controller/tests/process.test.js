import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

const serverEntry = fileURLToPath(new URL('../src/server.js', import.meta.url));
const children = new Set();

afterEach(async () => {
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
  await Promise.all([...children].map((child) => child.exitCode !== null || child.signalCode !== null
    ? Promise.resolve() : once(child, 'exit')));
  children.clear();
});

function launch(dataDirectory) {
  const child = spawn(process.execPath, [serverEntry, '--data-dir', dataDirectory,
    '--port', '0', '--step-delay-ms', '10000'], {
    cwd: path.dirname(path.dirname(serverEntry)),
    env: { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.add(child);
  let stderr = '';
  child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
  const lines = readline.createInterface({ input: child.stdout });
  const listening = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`controller did not start: ${stderr}`)), 5_000);
    lines.once('line', (line) => {
      clearTimeout(timer);
      try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
    });
    child.once('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`controller exited before listening (${code}): ${stderr}`));
    });
  });
  return { child, stderr: () => stderr, listening, lines };
}

async function establish(info) {
  const base = `http://127.0.0.1:${info.port}`;
  const token = fs.readFileSync(info.bootstrap_file, 'utf8').trim();
  const response = await fetch(`${base}/setup-api/session`, {
    method: 'POST', headers: { origin: base, authorization: `Bearer ${token}`,
      'content-type': 'application/json' }, body: '{}',
  });
  assert.equal(response.status, 201);
  return { base, cookie: response.headers.get('set-cookie').split(';', 1)[0],
    csrf: (await response.json()).csrf_token };
}

async function post(api, route, body = {}) {
  return fetch(`${api.base}${route}`, { method: 'POST',
    headers: { origin: api.base, cookie: api.cookie,
      'x-speechcraft-csrf': api.csrf, 'content-type': 'application/json' },
    body: JSON.stringify(body) });
}

test('real controller crash is reconciled as interrupted after restart', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-controller-process-'));
  const first = launch(directory);
  const firstInfo = await first.listening;
  const firstApi = await establish(firstInfo);
  const started = await post(firstApi, '/setup-api/start');
  assert.equal(started.status, 202);
  const oldJobId = (await started.json()).job.id;
  const running = await fetch(`${firstApi.base}/setup-api/status`, {
    headers: { cookie: firstApi.cookie },
  }).then((response) => response.json());
  assert.equal(running.job.state, 'running');
  first.child.kill('SIGKILL');
  await once(first.child, 'exit');
  children.delete(first.child);

  const second = launch(directory);
  const secondInfo = await second.listening;
  const secondApi = await establish(secondInfo);
  const recovered = await fetch(`${secondApi.base}/setup-api/status`, {
    headers: { cookie: secondApi.cookie },
  }).then((response) => response.json());
  assert.equal(recovered.job.id, oldJobId);
  assert.equal(recovered.job.state, 'interrupted');
  assert.equal(recovered.job.error.code, 'CONTROLLER_RESTARTED');
  assert.equal(recovered.production_ready, false);
  const exit = once(second.child, 'exit');
  second.child.kill('SIGTERM');
  await exit;
  children.delete(second.child);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('a second real controller process cannot acquire the same installation', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-controller-lock-'));
  const first = launch(directory);
  await first.listening;
  const second = spawn(process.execPath, [serverEntry, '--data-dir', directory,
    '--port', '0'], { cwd: path.dirname(path.dirname(serverEntry)),
    env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'] });
  children.add(second);
  let stderr = '';
  second.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
  const [code] = await once(second, 'exit');
  assert.equal(code, 1);
  assert.match(stderr, /CONTROLLER_ALREADY_RUNNING/);
  const firstExit = once(first.child, 'exit');
  first.child.kill('SIGTERM');
  await firstExit;
  children.delete(first.child);
  children.delete(second);
  fs.rmSync(directory, { recursive: true, force: true });
});
