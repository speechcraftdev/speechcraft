import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { assertLoopbackAddress, chooseDestination, formatFrontendReadyEvent, openDefaultBrowser, parseArgs, parseFrontendReadyEvent, readOrCreateFrontendIdentity, resolveLaunchContext, stopOwned, stopOwnedRecord, verifyFrontendOwnership } from '../src/launcher.js';
import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import { ownedProcessIdentity } from '../../setup-controller/src/bootstrap.js';
import { persistLauncherOwnership } from '../src/launcher.js';
import { REQUIRED_RUNTIME_FILES, validateRuntimeInventory } from '../src/package-inventory.js';

test('fresh and interrupted setup routes to setup; completed onboarding routes to the application', () => {
  assert.equal(chooseDestination(null), '/setup');
  assert.equal(chooseDestination({ completed: false }), '/setup');
  assert.equal(chooseDestination({ completed: true }), '/login');
});

test('frontend readiness is a versioned loopback event and never carries secrets', () => {
  const line = formatFrontendReadyEvent({ url: 'http://127.0.0.1:3002/setup', route: '/setup', ready: true });
  assert.equal(parseFrontendReadyEvent(line).route, '/setup');
  assert.equal(parseFrontendReadyEvent(formatFrontendReadyEvent({ url: 'http://127.0.0.1:3002/login', route: '/login', ready: true })).url,
    'http://127.0.0.1:3002/login');
  assert.equal(parseFrontendReadyEvent('SpeechCraft is available at http://127.0.0.1:3002/setup'), null);
  assert.equal(parseFrontendReadyEvent('SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3002/setup","route":"/setup","ready":false}'), null);
  assert.equal(parseFrontendReadyEvent('SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://example.com/setup","route":"/setup","ready":true}'), null);
  assert.equal(parseFrontendReadyEvent('SPEECHCRAFT_READY {"event":"frontend_ready","protocol":1,"url":"http://127.0.0.1:3002/setup","route":"/setup","ready":true,"token":"secret"}'), null);
  assert.throws(() => formatFrontendReadyEvent({ url: 'http://127.0.0.1:4317/setup-api', route: '/setup', ready: true }), /untrusted frontend URL/);
  assert.doesNotMatch(line, /token|cookie|csrf|bootstrap/i);
});

test('launcher arguments are explicit and reject unknown options', () => {
  assert.deepEqual(parseArgs(['--development', '--simulated', '--no-browser', '--data-dir', '/tmp/sc-data']), {
    command: 'start', dataDirectory: '/tmp/sc-data', development: true, simulated: true, openBrowser: false, json: false,
  });
  assert.deepEqual(parseArgs(['status', '--json', '--data-dir', '/tmp/sc-data']), {
    command: 'status', dataDirectory: '/tmp/sc-data', development: false, simulated: false, openBrowser: true, json: true,
  });
  assert.equal(parseArgs(['stop']).command, 'stop');
  assert.throws(() => parseArgs(['--bind', '0.0.0.0']), /Unknown launcher option/);
  assert.deepEqual(parseArgs(['--help']).help, true);
});

test('browser opener selects safe platform-native commands and avoids headless attempts', async () => {
  const calls = [];
  const spawnImpl = (command, args, options) => {
    calls.push({ command, args, options });
    const child = new EventEmitter(); child.unref = () => {};
    queueMicrotask(() => child.emit('spawn'));
    return child;
  };
  assert.equal(await openDefaultBrowser('http://127.0.0.1:3002/setup', { platform: 'linux', env: {}, spawnImpl }), false);
  assert.equal(calls.length, 0);
  assert.equal(await openDefaultBrowser('http://127.0.0.1:3002/login', { platform: 'darwin', env: {}, spawnImpl }), true);
  assert.equal(calls[0].command, 'open');
  assert.equal(calls[0].options.shell, false);
  assert.equal(await openDefaultBrowser('http://127.0.0.1:3002/setup', { platform: 'linux', wsl: true, env: {}, spawnImpl }), true);
  assert.equal(calls[1].command, 'wslview');
  await assert.rejects(openDefaultBrowser('http://example.com', { platform: 'darwin', env: {}, spawnImpl }), /non-SpeechCraft local URL/);
});

test('WSL launch context ignores Windows cache overrides and keeps a native data directory', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-launch-context-'));
  const mounts = `1 1 0:1 / ${root.replaceAll(' ', '\\040')} rw - ext4 /dev/sda rw\n2 1 0:2 / /mnt/c rw - 9p drvfs rw`;
  const host = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true };
  const resolved = resolveLaunchContext({
    env: {
      HF_HOME: '/mnt/c/Users/test/.cache/huggingface',
      TMPDIR: '/mnt/c/Temp',
      UV_CACHE_DIR: '/mnt/c/uv',
      PATH: '/usr/bin:/mnt/c/Windows/System32',
      XDG_DATA_HOME: '/mnt/c/Users/test/share',
    },
    host, home: root, mountInfoText: mounts,
  });
  assert.equal(resolved.env.HF_HOME, undefined);
  assert.equal(resolved.env.TMPDIR, undefined);
  assert.equal(resolved.env.PATH, '/usr/bin');
  assert.equal(resolved.dataDirectory, path.join(root, '.local', 'share', 'speechcraft', 'setup-controller'));
  assert.throws(() => resolveLaunchContext({
    env: { SPEECHCRAFT_SETUP_DATA_DIR: '/mnt/c/SpeechCraft' }, host, home: root, mountInfoText: mounts,
  }), (error) => error.code === 'WSL_WINDOWS_MOUNT');
  const native = resolveLaunchContext({
    dataDirectory: root, env: { HF_HOME: '/mnt/c/hidden' }, host: { ...host, wsl: false }, home: root,
  });
  assert.equal(native.dataDirectory, root);
  assert.equal(native.env.HF_HOME, '/mnt/c/hidden');
});

test('privileged local launch requires an explicit loopback bind', () => {
  assert.doesNotThrow(() => assertLoopbackAddress('127.0.0.1'));
  assert.throws(() => assertLoopbackAddress('0.0.0.0'), /bind explicitly to 127.0.0.1/);
});

test('frontend identity is a private random installation secret and remains stable for legitimate reuse', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-launcher-identity-'));
  try {
    fs.chmodSync(directory, 0o700);
    const first = readOrCreateFrontendIdentity(directory);
    const second = readOrCreateFrontendIdentity(directory);
    assert.match(first, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(second, first);
    const info = fs.statSync(path.join(directory, 'frontend.identity'));
    assert.equal(info.mode & 0o777, 0o600);
    assert.notEqual(first, requirePathDerivedIdentity(directory));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

function requirePathDerivedIdentity(directory) {
  // Deterministic path hashes are public diagnostic identities, never trust credentials.
  return Buffer.from(directory).toString('base64url').slice(0, 43);
}

test('a forged public frontend health response cannot satisfy private ownership verification', async () => {
  const originalFetch = globalThis.fetch;
  const identity = 'A'.repeat(43);
  const requests = [];
  globalThis.fetch = async (input, options = {}) => {
    requests.push({ url: String(input), headers: options.headers || {} });
    if (String(input).endsWith('/health')) return Response.json({ service: 'speechcraft-frontend', installation_id: 'f'.repeat(64) });
    return new Response(null, { status: 404 });
  };
  try {
    await assert.rejects(verifyFrontendOwnership('http://127.0.0.1:3002', identity), /Private frontend identity did not match/);
    assert.equal(requests.length, 2);
    assert.equal(requests[0].url.includes(identity), false);
    assert.equal(JSON.stringify(requests[1]).includes(identity), false, 'the challenge never sends the private identity to an unverified listener');
  } finally { globalThis.fetch = originalFetch; }
});

test('the launcher accepts a valid nonce proof from the matching frontend instance', async () => {
  const originalFetch = globalThis.fetch;
  const identity = 'B'.repeat(43);
  globalThis.fetch = async (input, options = {}) => {
    if (String(input).endsWith('/health')) return Response.json({ service: 'speechcraft-frontend' });
    const challenge = JSON.parse(options.body).challenge;
    const proof = (await import('node:crypto')).createHmac('sha256', Buffer.from(identity, 'base64url')).update(challenge).digest('base64url');
    return Response.json({ proof });
  };
  try { assert.equal(await verifyFrontendOwnership('http://127.0.0.1:3002', identity), true); }
  finally { globalThis.fetch = originalFetch; }
});

test('owned child shutdown rechecks the Darwin nonce and never signals without it', async () => {
  const nonce = 'launch-specific-nonce';
  let finish;
  const child = { pid: 8123, exitCode: null, signalCode: null };
  const handle = { child, label: 'macOS fixture', nonce,
    identity: Promise.resolve(`darwin:Thu Oct 16 10:00:00 2026:8123:${nonce}`),
    exit: new Promise((resolve) => { finish = resolve; }) };
  const signals = [];
  const identityResolver = (pid, suppliedNonce) => pid === child.pid && suppliedNonce === nonce
    ? `darwin:Thu Oct 16 10:00:00 2026:8123:${nonce}` : null;
  await stopOwned(handle, { identityResolver, signalGroup: (pid, signal) => {
    signals.push({ pid, signal }); child.exitCode = 0; finish({ code: 0, signal: null });
  } });
  assert.deepEqual(signals, [{ pid: child.pid, signal: 'SIGTERM' }]);

  const missing = { ...handle, child: { pid: 8124, exitCode: null, signalCode: null }, nonce: undefined,
    identity: Promise.resolve(null), exit: new Promise(() => {}) };
  await assert.rejects(stopOwned(missing, { identityResolver: () => null, signalGroup: () => assert.fail('must not signal without verified identity') }), /identity changed/);
});

test('persisted owned process record stops a live child by identity and confirms its process group exited', async () => {
  const nonce = 'd'.repeat(48);
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, shell: false,
    stdio: 'ignore', env: { ...process.env, SPEECHCRAFT_OWNED_PROCESS_NONCE: nonce } });
  await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  const closed = new Promise((resolve) => child.once('close', resolve));
  const identity = ownedProcessIdentity(child.pid, nonce);
  assert.ok(identity);
  const result = await stopOwnedRecord({ label: 'frontend fixture', pid: child.pid, nonce, identity, process_group_id: child.pid });
  assert.equal(result, 'stopped');
  await closed;
  assert.equal(ownedProcessIdentity(child.pid, nonce), null);
});

test('runtime package inventory requires the production preflight, runtime probe, and manifest inputs', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-inventory-'));
  try {
    for (const relative of REQUIRED_RUNTIME_FILES) {
      const filename = path.join(root, relative);
      fs.mkdirSync(path.dirname(filename), { recursive: true });
      fs.writeFileSync(filename, 'fixture');
    }
    const runtimeManifest = {
      worker: { validation_helper: 'workers/dataset/scripts/preflight.py' },
      runtime_validation: {
        probes: [{ helper: 'workers/dataset/scripts/runtime_probe.py' }],
        fixtures: { sample: { path: 'workers/dataset/runtime-fixtures/speech.wav' } },
      },
    };
    fs.writeFileSync(path.join(root, 'setup-controller/resources/runtime-manifest.json'), JSON.stringify(runtimeManifest));
    assert.equal(validateRuntimeInventory(root).ok, true);
    fs.unlinkSync(path.join(root, 'workers/dataset/scripts/preflight.py'));
    const missingPreflight = validateRuntimeInventory(root);
    assert.equal(missingPreflight.ok, false);
    assert.ok(missingPreflight.missing.includes('workers/dataset/scripts/preflight.py'));
    fs.writeFileSync(path.join(root, 'workers/dataset/scripts/preflight.py'), 'fixture');
    fs.unlinkSync(path.join(root, 'workers/dataset/scripts/runtime_probe.py'));
    assert.ok(validateRuntimeInventory(root).missing.includes('workers/dataset/scripts/runtime_probe.py'));
    fs.writeFileSync(path.join(root, 'workers/dataset/scripts/runtime_probe.py'), 'fixture');
    fs.writeFileSync(path.join(root, 'setup-controller/resources/runtime-manifest.json'), JSON.stringify({
      worker: { validation_helper: '../outside.py' }, runtime_validation: { probes: [], fixtures: {} },
    }));
    assert.equal(validateRuntimeInventory(root).ok, false, 'path traversal in trusted references is rejected');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('launcher ownership reconciliation preserves verified surviving services when a replacement starts', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-ownership-merge-'));
  try {
    fs.chmodSync(directory, 0o700);
    const file = path.join(directory, 'launcher.processes.json');
    const old = { label: 'setup controller', pid: 41001, nonce: 'controller-nonce', identity: 'identity-controller', process_group_id: 41001 };
    fs.writeFileSync(file, JSON.stringify({ schema_version: 1, records: [old] }), { mode: 0o600 });
    const identities = new Map([[41001, 'identity-controller'], [41002, 'identity-frontend']]);
    const replacement = { label: 'Next.js frontend', nonce: 'frontend-nonce', identityValue: 'identity-frontend', child: { pid: 41002 } };
    persistLauncherOwnership(directory, [replacement], { identityResolver: (pid) => identities.get(pid) || null,
      processGroupResolver: (pid) => identities.has(pid) });
    const records = JSON.parse(fs.readFileSync(file, 'utf8')).records;
    assert.deepEqual(records.map((record) => record.label).sort(), ['Next.js frontend', 'setup controller']);
    assert.equal(records.find((record) => record.pid === old.pid).nonce, old.nonce);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('ownership reconciliation preserves prior records and fails closed on identity uncertainty', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-ownership-uncertain-'));
  try {
    fs.chmodSync(directory, 0o700);
    const file = path.join(directory, 'launcher.processes.json');
    const old = { label: 'setup controller', pid: 42001, nonce: 'old-nonce', identity: 'expected-identity', process_group_id: 42001 };
    const original = `${JSON.stringify({ schema_version: 1, records: [old] })}\n`;
    fs.writeFileSync(file, original, { mode: 0o600 });
    const replacement = { label: 'Next.js frontend', nonce: 'new-nonce', identityValue: 'new-identity', child: { pid: 42002 } };
    assert.throws(() => persistLauncherOwnership(directory, [replacement], {
      identityResolver: () => null, processGroupResolver: () => true, timeoutMs: 50,
    }), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
    assert.equal(fs.readFileSync(file, 'utf8'), original, 'uncertain existing ownership remains intact');
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

test('persisted process record with uncertain identity is not signalled', async () => {
  const nonce = 'e'.repeat(48);
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, shell: false,
    stdio: 'ignore', env: { ...process.env, SPEECHCRAFT_OWNED_PROCESS_NONCE: nonce } });
  await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
  const closed = new Promise((resolve) => child.once('close', resolve));
  let signalled = false;
  try {
    await assert.rejects(stopOwnedRecord({ label: 'uncertain fixture', pid: child.pid, nonce, identity: 'different', process_group_id: child.pid }, {
      identityResolver: () => null, signalGroup: () => { signalled = true; },
    }), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
    assert.equal(signalled, false);
  } finally {
    child.kill('SIGTERM');
    await closed;
  }
});
