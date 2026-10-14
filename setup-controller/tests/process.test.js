import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';
import { StateStore, StateStoreError } from '../src/state-store.js';

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

test('a process killed immediately after acquiring the lock is safely detected and replaced', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-lock-owner-crash-'));
  const code = `
    import { StateStore } from ${JSON.stringify(new URL('../src/state-store.js', import.meta.url).href)};
    StateStore.open(process.env.SPEECHCRAFT_TEST_DIR);
    process.stdout.write('owner-acquired\\n');
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
  `;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code], {
    cwd: path.dirname(path.dirname(serverEntry)), env: { ...process.env, SPEECHCRAFT_TEST_DIR: directory },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.add(child);
  const lines = readline.createInterface({ input: child.stdout });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('child did not acquire ownership')), 5_000);
    lines.once('line', (line) => {
      clearTimeout(timer);
      if (line !== 'owner-acquired') reject(new Error(`unexpected child output: ${line}`));
      else resolve();
    });
  });
  child.kill('SIGKILL');
  await once(child, 'exit');
  children.delete(child);
  const recovered = StateStore.open(directory);
  assert.equal(recovered.snapshot().revision, 0);
  recovered.releaseOwnership();
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

function observeStartup(child, timeoutMs = 5_000) {
  return new Promise((resolve, reject) => {
    let stderr = '';
    child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
    const lines = readline.createInterface({ input: child.stdout });
    const timer = setTimeout(() => reject(new Error(`controller startup timed out: ${stderr}`)), timeoutMs);
    lines.once('line', (line) => {
      clearTimeout(timer);
      try { resolve({ kind: 'line', value: JSON.parse(line), stderr }); }
      catch (error) { reject(error); }
    });
    child.once('exit', (code, signal) => {
      clearTimeout(timer);
      resolve({ kind: 'exit', code, signal, stderr });
    });
  });
}

function writeStaleOwner(directory) {
  fs.writeFileSync(path.join(directory, '.controller.lock'), JSON.stringify({
    version: 1, pid: 2_000_000_000, identity: 'linux:dead-boot:1',
    nonce: 'c'.repeat(48), createdAt: new Date().toISOString(),
  }), { mode: 0o600 });
}

test('multiple real processes competing for one stale lock produce one owner', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-stale-race-'));
  writeStaleOwner(directory);
  const args = [serverEntry, '--data-dir', directory, '--port', '0'];
  const options = { cwd: path.dirname(path.dirname(serverEntry)), env: { ...process.env },
    stdio: ['ignore', 'pipe', 'pipe'] };
  const first = spawn(process.execPath, args, options);
  const second = spawn(process.execPath, args, options);
  children.add(first); children.add(second);
  const outcomes = await Promise.all([observeStartup(first), observeStartup(second)]);
  const owners = outcomes.filter((outcome) => outcome.kind === 'line'
    && outcome.value.event === 'listening');
  assert.equal(owners.length, 1, JSON.stringify(outcomes));
  const loser = outcomes.find((outcome) => outcome.kind === 'exit');
  assert.equal(loser.code, 1);
  assert.match(loser.stderr, /CONTROLLER_ALREADY_RUNNING|LOCK_RECOVERY_REQUIRED/);
  const winner = owners[0] === outcomes[0] ? first : second;
  const exit = once(winner, 'exit');
  winner.kill('SIGTERM');
  await exit;
  children.delete(first); children.delete(second);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('process killed during stale ownership recovery leaves a safe manual-recovery tombstone', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-recovery-crash-'));
  writeStaleOwner(directory);
  const code = `
    import fs from 'node:fs';
    import { StateStore } from ${JSON.stringify(new URL('../src/state-store.js', import.meta.url).href)};
    const original = fs.openSync;
    fs.openSync = function(file, ...args) {
      const fd = original.call(fs, file, ...args);
      if (String(file).endsWith('.controller.recovery')) {
        process.stdout.write('recovery-marker-created\\n');
        Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0);
      }
      return fd;
    };
    StateStore.open(process.env.SPEECHCRAFT_TEST_DIR);
  `;
  const child = spawn(process.execPath, ['--input-type=module', '-e', code], {
    cwd: path.dirname(path.dirname(serverEntry)), env: { ...process.env, SPEECHCRAFT_TEST_DIR: directory },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.add(child);
  const lines = readline.createInterface({ input: child.stdout });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('recovery child did not create the recovery marker')), 5_000);
    lines.once('line', (line) => {
      clearTimeout(timer);
      if (line !== 'recovery-marker-created') reject(new Error(`unexpected child output: ${line}`));
      else resolve();
    });
  });
  child.kill('SIGKILL');
  await once(child, 'exit');
  children.delete(child);
  assert.equal(fs.existsSync(path.join(directory, '.controller.recovery')), true);
  assert.throws(() => StateStore.open(directory), (error) => error instanceof StateStoreError
    && error.code === 'LOCK_RECOVERY_REQUIRED');
  fs.rmSync(directory, { recursive: true, force: true });
});
