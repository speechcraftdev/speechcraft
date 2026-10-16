import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { assertLoopbackAddress, chooseDestination, openDefaultBrowser, parseArgs, readOrCreateFrontendIdentity, stopOwned, verifyFrontendOwnership } from '../src/launcher.js';
import { EventEmitter } from 'node:events';

test('fresh and interrupted setup routes to setup; completed onboarding routes to the application', () => {
  assert.equal(chooseDestination(null), '/setup');
  assert.equal(chooseDestination({ completed: false }), '/setup');
  assert.equal(chooseDestination({ completed: true }), '/login');
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
