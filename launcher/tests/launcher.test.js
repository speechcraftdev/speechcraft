import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { assertLoopbackAddress, chooseDestination, parseArgs, readOrCreateFrontendIdentity, verifyFrontendOwnership } from '../src/launcher.js';

test('fresh and interrupted setup routes to setup; completed onboarding routes to the application', () => {
  assert.equal(chooseDestination(null), '/setup');
  assert.equal(chooseDestination({ completed: false }), '/setup');
  assert.equal(chooseDestination({ completed: true }), '/login');
});

test('launcher arguments are explicit and reject unknown options', () => {
  assert.deepEqual(parseArgs(['--development', '--simulated', '--no-browser', '--data-dir', '/tmp/sc-data']), {
    dataDirectory: '/tmp/sc-data', development: true, simulated: true, openBrowser: false,
  });
  assert.throws(() => parseArgs(['--bind', '0.0.0.0']), /Unknown launcher option/);
  assert.deepEqual(parseArgs(['--help']).help, true);
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
