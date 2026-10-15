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
import { runOwnedProcess } from '../src/bootstrap.js';
import { createSetupController } from '../src/server.js';

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

for (const stage of ['install_python', 'install_model_manager']) {
  test(`controller killed during ${stage} is reconciled before a simulated restart`, { skip: process.platform === 'win32' }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), `speechcraft-crash-${stage}-`));
    const sentinel = path.join(directory, 'previously-verified-runtime.txt');
    fs.writeFileSync(sentinel, 'verified installation remains intact');
    const workerCode = `
      import fs from 'node:fs';
      import { StateStore } from ${JSON.stringify(new URL('../src/state-store.js', import.meta.url).href)};
      import { runOwnedProcess } from ${JSON.stringify(new URL('../src/bootstrap.js', import.meta.url).href)};
      const directory = process.argv[1], stage = process.argv[2];
      const store = StateStore.open(directory);
      const attemptId = '00000000-0000-4000-8000-000000000002';
      store.commit((state) => { state.job = { schema_version: 2, id: '00000000-0000-4000-8000-000000000001',
        attempt_id: attemptId, mode: 'bootstrap', state: 'running', stage, task_id: 'bootstrap:' + stage,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(), started_at: new Date().toISOString(),
        completed_at: null, attempt_count: 1, revision: 0, progress: null, bootstrap_status: null, error: null }; }, 'stage_started');
      const writeFile = directory + '/active-writer.log';
      await store.commit((state) => { state.job.child_process = { attempt_id: attemptId, state: 'starting',
        pid: null, identity: null, termination_status: 'active' }; }, 'writer_starting');
      const writer = String.raw\`const fs = require('node:fs'); const file = process.argv[1];
        setInterval(() => fs.appendFileSync(file, '.'), 10);\`;
      const operation = runOwnedProcess(process.execPath, ['-e', writer, writeFile], { cwd: directory, env: process.env,
        onChildProcess: (record) => store.commit((state) => { state.job.child_process = record
          ? { ...record, attempt_id: attemptId } : null; }, record ? 'writer_active' : 'writer_stopped') });
      const deadline = Date.now() + 3000;
      while (!fs.existsSync(writeFile) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
      process.stdout.write('writer-ready\\n');
      await operation;
    `;
    const controllerProcess = spawn(process.execPath, ['--input-type=module', '-e', workerCode, directory, stage], {
      cwd: path.dirname(path.dirname(serverEntry)), env: { ...process.env }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    children.add(controllerProcess);
    let output = '';
    controllerProcess.stdout.setEncoding('utf8').on('data', (chunk) => { output += chunk; });
    try {
      const deadline = Date.now() + 5_000;
      while (!output.includes('writer-ready') && Date.now() < deadline && controllerProcess.exitCode === null) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
      assert.match(output, /writer-ready/);
      controllerProcess.kill('SIGKILL');
      await once(controllerProcess, 'exit');
      children.delete(controllerProcess);

      const app = await createSetupController({ dataDirectory: directory, mode: 'simulated', port: 0 });
      try {
        const recovered = app.store.snapshot().job;
        assert.equal(recovered.state, 'interrupted');
        assert.equal(recovered.stage, stage);
        assert.equal(recovered.mode, 'bootstrap');
        assert.equal(recovered.child_process, null);
        assert.equal(recovered.termination_status, 'termination_confirmed');
        assert.throws(() => app.controller.retry(), { code: 'EXECUTOR_MODE_MISMATCH' });
        const writerSize = fs.statSync(path.join(directory, 'active-writer.log')).size;
        await new Promise((resolve) => setTimeout(resolve, 80));
        assert.equal(fs.statSync(path.join(directory, 'active-writer.log')).size, writerSize,
          'the prior writer must be stopped before the new controller accepts work');
        assert.equal(fs.readFileSync(sentinel, 'utf8'), 'verified installation remains intact');
      } finally { await app.close(); }
    } finally {
      if (controllerProcess.exitCode === null && controllerProcess.signalCode === null) controllerProcess.kill('SIGKILL');
      fs.rmSync(directory, { recursive: true, force: true });
    }
  });
}

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

test('credential is delivered only after durable process ownership acknowledgement', async () => {
  const marker = 'hf_SECRET_MARKER_ownership_gate_123456';
  let persisted = false;
  const order = [];
  const result = await runOwnedProcess(process.execPath, ['-e',
    `let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',x=>s+=x);process.stdin.on('end',()=>process.stdout.write(s===${JSON.stringify(marker)}?'received':'empty'))`], {
    cwd: os.tmpdir(), env: { PATH: process.env.PATH }, stdinData: Buffer.from(marker),
    onChildProcess: async (record) => {
      if (record?.pid) { order.push('persisting'); await new Promise((resolve) => setTimeout(resolve, 25)); persisted = true; order.push('persisted'); }
    },
    onOutput: (_stream, chunk) => { if (chunk.toString() === 'received') { assert.equal(persisted, true); order.push('consumed'); } },
  });
  assert.equal(result.stdout, 'received');
  assert.ok(order.indexOf('persisting') < order.indexOf('persisted'));
  assert.ok(order.indexOf('persisted') < order.indexOf('consumed'));
  assert.ok(order.filter((value) => value === 'persisting').length >= 1);
  assert.equal(JSON.stringify(result).includes(marker), false);
});

test('failed ownership persistence or unavailable process identity withholds credentials', async () => {
  for (const identityResolver of [undefined, () => null]) {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-secret-gate-'));
    const received = path.join(directory, 'received');
    const exited = path.join(directory, 'exited');
    const marker = 'hf_SECRET_MARKER_must_not_be_consumed_123456';
    const childCode = `const fs=require('node:fs');let s='';process.stdin.on('data',b=>s+=b);process.stdin.on('end',()=>{if(s)fs.writeFileSync(${JSON.stringify(received)},s);fs.writeFileSync(${JSON.stringify(exited)},'closed')});`;
    let writes = 0;
    let childPid = null;
    const options = { cwd: directory, env: { PATH: process.env.PATH }, stdinData: Buffer.from(marker), graceMs: 100,
      ...(identityResolver ? { identityResolver } : {}),
      onChildProcess: async (record) => { writes += 1; if (record?.pid) childPid = record.pid;
        if (record?.pid && writes === 1 && !identityResolver) throw new Error('state write failed'); } };
    await assert.rejects(runOwnedProcess(process.execPath, ['-e', childCode], options),
      (error) => ['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'BOOTSTRAP_OWNERSHIP_PERSIST_FAILED'].includes(error.code));
    const deadline = Date.now() + 2_000;
    while (!fs.existsSync(exited) && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 5));
    assert.equal(fs.existsSync(received), false);
    if (identityResolver) assert.ok(fs.existsSync(exited), 'an unidentifiable child may exit only through the closed input pipe');
    else assert.throws(() => process.kill(childPid, 0), { code: 'ESRCH' }, 'a child with valid identity is terminated after ownership persistence fails');
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('cancellation before process creation and between spawn and ownership persistence cannot release a token', async () => {
  const before = new AbortController(); before.abort();
  await assert.rejects(runOwnedProcess(process.execPath, ['-e', 'process.exit(0)'], {
    signal: before.signal, stdinData: Buffer.from('hf_SECRET_MARKER_pre_spawn_123456'),
    onChildProcess: () => assert.fail('pre-aborted execution must not spawn') }), { code: 'BOOTSTRAP_CANCELLED' });

  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-secret-cancel-'));
  const received = path.join(directory, 'received');
  const childCode = `const fs=require('node:fs');let s='';process.stdin.on('data',b=>s+=b);process.stdin.on('end',()=>{if(s)fs.writeFileSync(${JSON.stringify(received)},s)});`;
  const abort = new AbortController();
  let signalPersistStarted;
  const persistStarted = new Promise((resolve) => { signalPersistStarted = resolve; });
  let release;
  const persistenceGate = new Promise((resolve) => { release = resolve; });
  const operation = runOwnedProcess(process.execPath, ['-e', childCode], {
    cwd: directory, env: { PATH: process.env.PATH }, stdinData: Buffer.from('hf_SECRET_MARKER_cancel_123456'),
    signal: abort.signal, graceMs: 100,
    onChildProcess: async (record) => { if (record?.pid) { signalPersistStarted(); await persistenceGate; } },
  });
  await persistStarted;
  abort.abort();
  release();
  await assert.rejects(operation, (error) => ['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code));
  assert.equal(fs.existsSync(received), false);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('an obsolete setup attempt cannot acknowledge ownership or receive the credential', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-secret-obsolete-attempt-'));
  const received = path.join(directory, 'received');
  let childPid = null;
  const code = `const fs=require('node:fs');let data='';process.stdin.on('data',b=>data+=b);process.stdin.on('end',()=>{if(data)fs.writeFileSync(${JSON.stringify(received)},data)});`;
  await assert.rejects(runOwnedProcess(process.execPath, ['-e', code], {
    cwd: directory, env: { PATH: process.env.PATH }, graceMs: 100,
    stdinData: Buffer.from('hf_SECRET_MARKER_obsolete_attempt_123456'),
    onChildProcess: (record) => { if (record?.pid) childPid = record.pid; return false; },
  }), (error) => ['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code));
  assert.equal(fs.existsSync(received), false);
  assert.throws(() => process.kill(childPid, 0), { code: 'ESRCH' });
  fs.rmSync(directory, { recursive: true, force: true });
});
