import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { assertLoopbackAddress, chooseDestination, formatFrontendReadyEvent, openDefaultBrowser, parseArgs, parseFrontendReadyEvent, readOrCreateFrontendIdentity, resolveLaunchContext, stopOwned, stopOwnedRecord, verifyFrontendOwnership, waitForRestoration } from '../src/launcher.js';
import { classifyChildStreamLine, createLauncherLog, createLaunchLogSession, formatLaunchAccess, formatRemoteAccessGuidance, isRoutineChildStdout, launchOutputMode, redactLaunchText } from '../src/launch-log.js';
import { PassThrough } from 'node:stream';
import { EventEmitter, once } from 'node:events';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { ownedProcessIdentity } from '../../setup-controller/src/bootstrap.js';
import { persistLauncherOwnership } from '../src/launcher.js';
import { REQUIRED_RUNTIME_FILES, validateRuntimeInventory } from '../src/package-inventory.js';

test('fresh and interrupted setup routes to setup; completed onboarding routes to the application', () => {
  assert.equal(chooseDestination(null), '/setup');
  assert.equal(chooseDestination({ completed: false }), '/setup');
  assert.equal(chooseDestination({ completed: true }), '/login');
});

test('launcher waits through the initial service_start_pending status during real runtime revalidation', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  const pending = { components: { backend_service: { state: 'blocked', failure_code: 'service_start_pending' },
    runtime_probes: { state: 'pending' } } };
  const ready = { components: { backend_service: { state: 'verified', verification: 'internal_health_identity' },
    runtime_probes: { state: 'verified' } } };
  globalThis.fetch = async () => new Response(JSON.stringify(calls++ === 0 ? pending : ready),
    { status: 200, headers: { 'content-type': 'application/json' } });
  try {
    const result = await waitForRestoration({ cookie: 'session=fixture' }, 1000, null, 1);
    assert.equal(result.ready, true);
    assert.equal(result.status.components.backend_service.verification, 'internal_health_identity');
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
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
    command: 'start', dataDirectory: '/tmp/sc-data', development: true, simulated: true, openBrowser: false, json: false, verbose: false, yes: false, purge: false,
  });
  assert.deepEqual(parseArgs(['status', '--json', '--data-dir', '/tmp/sc-data']), {
    command: 'status', dataDirectory: '/tmp/sc-data', development: false, simulated: false, openBrowser: true, json: true, verbose: false, yes: false, purge: false,
  });
  assert.equal(parseArgs(['--verbose']).verbose, true);
  assert.equal(parseArgs(['stop']).command, 'stop');
  assert.equal(parseArgs(['uninstall', '--force', '--remove-data']).purge, true);
  assert.equal(parseArgs(['uninstall', '--yes']).yes, true);
  assert.throws(() => parseArgs(['--yes']), /Unknown launcher option/);
  assert.throws(() => parseArgs(['--bind', '0.0.0.0']), /Unknown launcher option/);
  assert.throws(() => parseArgs(['status', '--verbose']), /Unknown launcher option/);
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
    fs.mkdirSync(path.join(root, 'frontend'), { recursive: true });
    fs.writeFileSync(path.join(root, 'frontend/server.js'), 'fixture frontend');
    const frontendBytes = fs.readFileSync(path.join(root, 'frontend/server.js'));
    fs.writeFileSync(path.join(root, 'frontend-release-manifest.json'), JSON.stringify({ schema_version: 1, next_build_id: 'fixture', files: [
      { path: 'frontend/server.js', size_bytes: frontendBytes.length, sha256: createHash('sha256').update(frontendBytes).digest('hex') },
    ] }));
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

test('launch logs stay private, bounded, and free of secret values', () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-launch-log-'));
  const outside = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-launch-log-outside-'));
  const secret = 'bootstrap-secret-value';
  try {
    fs.chmodSync(directory, 0o700);
    fs.writeFileSync(path.join(directory, 'launcher.log'), 'x'.repeat(50), { mode: 0o644 });
    const log = createLauncherLog(directory, { limit: 80 });
    log.write('a'.repeat(40));
    log.write('y'.repeat(40));
    log.write('z'.repeat(40));
    log.close();
    const current = fs.readFileSync(log.path, 'utf8');
    const previous = fs.readFileSync(`${log.path}.1`, 'utf8');
    assert.equal(fs.statSync(log.path).mode & 0o777, 0o600);
    assert.equal(fs.statSync(`${log.path}.1`).mode & 0o777, 0o600);
    assert.ok(fs.statSync(log.path).size <= 80);
    assert.ok(fs.statSync(`${log.path}.1`).size <= 80);
    assert.equal(fs.readdirSync(directory).filter((name) => name.startsWith('launcher.log')).length, 2);
    fs.symlinkSync(path.join(outside, 'elsewhere.log'), path.join(directory, 'linked.log'));
    fs.writeFileSync(path.join(outside, 'elsewhere.log'), 'keep');
    fs.rmSync(log.path, { force: true });
    fs.symlinkSync(path.join(outside, 'elsewhere.log'), log.path);
    assert.throws(() => createLauncherLog(directory, { limit: 80 }), /regular file/);
    assert.equal(fs.readFileSync(path.join(outside, 'elsewhere.log'), 'utf8'), 'keep');
    assert.equal(redactLaunchText(`failed token=${secret} Bearer ${secret}`, [secret]).includes(secret), false);
    assert.match(redactLaunchText(`failed token=${secret}`, []), /\[redacted\]/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test('default launch output hides routine service banners and explains a remote session', () => {
  assert.equal(isRoutineChildStdout('{"event":"listening","host":"127.0.0.1","port":4317}'), true);
  assert.equal(isRoutineChildStdout('  ▲ Next.js 15.0.0'), true);
  assert.equal(isRoutineChildStdout('  - Local: http://127.0.0.1:3002'), true);
  assert.equal(isRoutineChildStdout('Error: listen EADDRINUSE'), false);
  assert.equal(classifyChildStreamLine('stdout', '{"event":"listening","port":4317}'), 'log');
  assert.equal(classifyChildStreamLine('stderr', 'Error: listen EADDRINUSE'), 'show');
  assert.equal(classifyChildStreamLine('stdout', ''), 'drop');
  assert.deepEqual(launchOutputMode({ stdoutIsTTY: true }), { verbose: false, emitReadyEvent: false });
  assert.deepEqual(launchOutputMode({ stdoutIsTTY: false }), { verbose: false, emitReadyEvent: true });
  assert.equal(launchOutputMode({ verbose: true, stdoutIsTTY: true }).emitReadyEvent, true);
  assert.equal(launchOutputMode({ env: { SPEECHCRAFT_LOG_LEVEL: 'debug' }, stdoutIsTTY: true }).verbose, true);
  const guidance = formatLaunchAccess({
    url: 'http://127.0.0.1:3002/setup',
    opened: false,
    env: { SSH_CONNECTION: '1', DISPLAY: '', WAYLAND_DISPLAY: '' },
  });
  assert.match(guidance, /did not open a browser/);
  assert.match(guidance, /ssh -N -L 3002:127\.0\.0\.1:3002 cozytent/);
  assert.match(guidance, /example SSH alias/);
  assert.match(guidance, /127\.0\.0\.1 only/);
  assert.doesNotMatch(guidance, /user@cozytent-host|0\.0\.0\.0|browser was opened on the server/);
  const opened = formatLaunchAccess({ url: 'http://127.0.0.1:3002/setup', opened: true, env: { DISPLAY: ':0' } });
  assert.match(opened, /asked this desktop to open/);
  assert.match(opened, /does not prove a window appeared/);
  assert.doesNotMatch(opened, /cozytent/);
  const localFailure = formatLaunchAccess({ url: 'http://127.0.0.1:3002/setup', opened: false, env: { DISPLAY: ':0' } });
  assert.match(localFailure, /could not open a desktop browser/);
  assert.doesNotMatch(localFailure, /cozytent|ssh -N/);
  const skipped = formatLaunchAccess({
    url: 'http://127.0.0.1:3002/setup', opened: false, browserRequested: false, env: { SSH_CONNECTION: '203.0.113.4 1 127.0.0.1 22' },
  });
  assert.match(skipped, /--no-browser/);
  assert.match(skipped, /example SSH alias/);
  assert.match(formatRemoteAccessGuidance('http://127.0.0.1:3002/login'), /http:\/\/127\.0\.0\.1:3002\/login/);
});

test('service logs stay in-process when the log file, pipe, or child fails', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-log-session-'));
  const secret = 'bootstrap-secret-value';
  const cli = fileURLToPath(new URL('../src/cli.js', import.meta.url));
  try {
    fs.chmodSync(directory, 0o700);
    fs.writeFileSync(path.join(directory, 'bootstrap.token'), `${secret}\n`, { mode: 0o600 });
    const terminal = [];
    const session = createLaunchLogSession(directory, { writeTerminal: (text) => terminal.push(text) });
    const stdout = new PassThrough();
    const stderr = new PassThrough();
    session.attach('stdout', stdout);
    session.attach('stderr', stderr);
    stdout.write('{"event":"listening","host":"127.0.0.1","port":4317}\n');
    stdout.write('✓ Ready in 1s\n');
    stderr.write('✓ Ready in 1s\n');
    stderr.write(`Error: ${secret} leaked token=${secret}\n`);
    const stdoutEnded = once(stdout, 'end');
    const stderrEnded = once(stderr, 'end');
    stdout.write('partial');
    stdout.end();
    stderr.end();
    await Promise.all([stdoutEnded, stderrEnded]);
    const stored = fs.readFileSync(session.path, 'utf8');
    const shown = terminal.join('');
    assert.equal(fs.statSync(session.path).mode & 0o777, 0o600);
    assert.match(stored, /"event":"listening"/);
    assert.match(stored, /partial/);
    assert.doesNotMatch(stored, new RegExp(secret));
    assert.match(stored, /\[redacted\]/);
    assert.doesNotMatch(shown, /listening/);
    assert.doesNotMatch(shown, /stdout-ready/);
    assert.match(shown, /✓ Ready in 1s/);
    assert.equal(shown.match(/✓ Ready in 1s/g).length, 1);
    assert.match(shown, /Error:/);
    assert.doesNotMatch(shown, new RegExp(secret));
    session.close();

    const verbose = [];
    const verboseSession = createLaunchLogSession(directory, { verbose: true, writeTerminal: (text) => verbose.push(text) });
    const verboseOut = new PassThrough();
    verboseSession.attach('stdout', verboseOut);
    const verboseEnded = once(verboseOut, 'end');
    verboseOut.end('{"event":"listening","port":4317}\n');
    await verboseEnded;
    assert.match(verbose.join(''), /listening/);
    verboseSession.close();

    const blocked = [];
    const blockedSession = createLaunchLogSession(directory, {
      openLog() { throw Object.assign(new Error('permission denied'), { code: 'EACCES' }); },
      writeTerminal: (text) => blocked.push(text),
    });
    assert.equal(blockedSession.fileEnabled, false);
    const blockedErr = new PassThrough();
    blockedSession.attach('stderr', blockedErr);
    const blockedEnded = once(blockedErr, 'end');
    blockedErr.end('Error: still visible\n');
    await blockedEnded;
    assert.match(blocked.join(''), /could not open the detailed log/);
    assert.match(blocked.join(''), /still visible/);
    blockedSession.close();

    const full = [];
    let writes = 0;
    const fullSession = createLaunchLogSession(directory, {
      openLog: () => ({
        path: path.join(directory, 'launcher.log'),
        write() { writes += 1; throw Object.assign(new Error('no space'), { code: 'ENOSPC' }); },
        close() {},
      }),
      writeTerminal: (text) => full.push(text),
    });
    const fullErr = new PassThrough();
    fullSession.attach('stderr', fullErr);
    const fullEnded = once(fullErr, 'end');
    fullErr.write('Error: first\n');
    fullErr.end('Error: second\n');
    await fullEnded;
    assert.equal(writes, 1);
    assert.match(full.join(''), /stopped writing the detailed log \(ENOSPC\)/);
    assert.match(full.join(''), /Error: first/);
    assert.match(full.join(''), /Error: second/);
    fullSession.close();

    const huge = [];
    const hugeSession = createLaunchLogSession(directory, { writeTerminal: (text) => huge.push(text) });
    const hugeErr = new PassThrough();
    hugeSession.attach('stderr', hugeErr);
    const hugeEnded = once(hugeErr, 'end');
    hugeErr.write(`${'E'.repeat(2 * 1024 * 1024)}`);
    hugeErr.end('\nError: after-huge\n');
    await hugeEnded;
    const hugeText = huge.join('');
    assert.match(hugeText, /truncated a long service line/);
    assert.match(hugeText, /after-huge/);
    assert.ok(hugeText.length < 64 * 1024);
    hugeSession.close();

    const crashed = [];
    const crashSession = createLaunchLogSession(directory, { writeTerminal: (text) => crashed.push(text) });
    const child = spawn(process.execPath, ['-e', 'console.error("crash-marker"); process.exit(2);'], { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    crashSession.attach('stdout', child.stdout);
    crashSession.attach('stderr', child.stderr);
    const [code] = await onceExit(child);
    assert.equal(code, 2);
    assert.match(crashed.join(''), /crash-marker/);
    crashSession.close();

    const helper = spawn(process.execPath, [cli, '--help'], {
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, SPEECHCRAFT_LAUNCHER_LOG_HELPER: '1' },
    });
    let help = '';
    helper.stdout.setEncoding('utf8').on('data', (chunk) => { help += chunk; });
    const [helpCode] = await onceExit(helper);
    assert.equal(helpCode, 0);
    assert.match(help, /Usage: speechcraft/);
    assert.match(help, /speechcraft uninstall/);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});

function onceExit(child) {
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code) => resolve([code]));
  });
}
