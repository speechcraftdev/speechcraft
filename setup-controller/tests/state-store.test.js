import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { StateStore, StateStoreError } from '../src/state-store.js';

const directories = new Set();
afterEach(() => {
  for (const directory of directories) fs.rmSync(directory, { recursive: true, force: true });
  directories.clear();
});

function directory() {
  const value = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-state-lock-'));
  directories.add(value);
  return value;
}

function writeLock(target, { pid = 2_000_000_000, identity = 'linux:old-boot:123' } = {}) {
  fs.writeFileSync(path.join(target, '.controller.lock'), JSON.stringify({
    version: 1, pid, identity, nonce: 'a'.repeat(48), createdAt: new Date().toISOString(),
  }), { mode: 0o600 });
}

test('stale owner can be safely recovered and ownership released and reacquired', () => {
  const target = directory();
  writeLock(target);
  const first = StateStore.open(target);
  assert.equal(first.snapshot().revision, 0);
  first.releaseOwnership();
  const second = StateStore.open(target);
  second.releaseOwnership();
});

test('PID reuse is detected by process start identity, not PID alone', () => {
  const target = directory();
  const oldIdentity = process.platform === 'darwin' ? 'darwin:old process start' : 'linux:old-boot:1';
  writeLock(target, { pid: process.pid, identity: oldIdentity });
  const store = StateStore.open(target);
  assert.equal(store.snapshot().revision, 0);
  store.releaseOwnership();
});

test('corrupt owner metadata fails closed and remains available for inspection', () => {
  const target = directory();
  fs.writeFileSync(path.join(target, '.controller.lock'), '{broken', { mode: 0o600 });
  assert.throws(() => StateStore.open(target), (error) => error instanceof StateStoreError
    && error.code === 'CONTROLLER_ALREADY_RUNNING' && /corrupt/.test(error.message));
  assert.equal(fs.readFileSync(path.join(target, '.controller.lock'), 'utf8'), '{broken');
});

test('a crash during stale-lock recovery leaves a fail-closed recovery marker', () => {
  const target = directory();
  writeLock(target);
  fs.writeFileSync(path.join(target, '.controller.recovery'), JSON.stringify({
    version: 1, pid: 2_000_000_000, identity: 'linux:dead:1', nonce: 'b'.repeat(48),
  }), { mode: 0o600 });
  assert.throws(() => StateStore.open(target), (error) => error instanceof StateStoreError
    && error.code === 'LOCK_RECOVERY_REQUIRED' && /inspect/.test(error.message));
  assert.equal(fs.existsSync(path.join(target, '.controller.lock')), true);
  assert.equal(fs.existsSync(path.join(target, '.controller.recovery')), true);
});

test('state schema v1 upgrades atomically with execution attempt identities', () => {
  const target = directory();
  const old = { schema_version: 1, revision: 1, job: {
    schema_version: 1, id: 'fce21296-97b5-44a4-b8e5-0eb6458bbbe1', state: 'interrupted',
    stage: 'inspect_system', task_id: 'simulated:inspect_system', created_at: '2026-01-01T00:00:00.000Z',
    updated_at: '2026-01-01T00:00:00.000Z', started_at: '2026-01-01T00:00:00.000Z',
    completed_at: '2026-01-01T00:00:00.000Z', attempt_count: 1, revision: 1,
    progress: null, error: null,
  }, events: [{ id: 1, type: 'job_started', at: '2026-01-01T00:00:00.000Z', data: { job: null } }] };
  fs.writeFileSync(path.join(target, 'state.json'), JSON.stringify(old), { mode: 0o600 });
  const store = StateStore.open(target);
  assert.equal(store.snapshot().schema_version, 2);
  assert.equal(store.snapshot().job.schema_version, 2);
  assert.match(store.snapshot().job.attempt_id, /^[0-9a-f-]{36}$/i);
  store.releaseOwnership();
});
