import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import test from 'node:test';
import { ownedProcessIdentity } from '../../setup-controller/src/bootstrap.js';
import { ownedChildEnvironment } from '../src/launcher.js';

test('Darwin ownership nonce remains visible after a child changes title and expands its environment', {
  skip: process.platform !== 'darwin',
}, async () => {
  const nonce = randomBytes(24).toString('hex');
  const env = ownedChildEnvironment({ ...process.env, SPEECHCRAFT_FILLER: 'x'.repeat(120_000) }, nonce);
  assert.equal(Object.keys(env)[0], 'SPEECHCRAFT_OWNED_PROCESS_NONCE');
  const source = `process.title='next-server (v16.2.1)'; for (let i=0;i<300;i++) process.env['SPEECHCRAFT_GROWN_'+i]='z'.repeat(128); setInterval(()=>{},1000)`;
  const child = spawn(process.execPath, ['-e', source], { env, detached: true, stdio: 'ignore' });
  try {
    await new Promise((resolve, reject) => { child.once('spawn', resolve); child.once('error', reject); });
    await new Promise((resolve) => setTimeout(resolve, 300));
    const initial = ownedProcessIdentity(child.pid, nonce);
    assert.ok(initial, 'the Darwin process identity should be observable after title/environment changes');
    await new Promise((resolve) => setTimeout(resolve, 250));
    assert.equal(ownedProcessIdentity(child.pid, nonce), initial);
  } finally {
    const exited = new Promise((resolve) => child.once('exit', resolve));
    try { process.kill(-child.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
    if (child.exitCode === null && child.signalCode === null) await exited;
  }
});
