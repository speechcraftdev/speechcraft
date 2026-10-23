import assert from 'node:assert/strict';
import test from 'node:test';
import { UvOutputProgress } from '../src/uv-output-progress.js';

test('uv 0.12.23 observed install records stream across chunks and stdout/stderr without raw output', () => {
  const events = [];
  const parser = new UvOutputProgress((event) => events.push(event), { minIntervalMs: 0 });
  parser.push('stderr', Buffer.from('Resolved 2 packages in 4ms\nPrepared 2 packages in 7ms\n + torch==2.8.0+cu128'));
  parser.push('stdout', Buffer.from('+ torchaudio==2.8.0+cu128\n'));
  parser.push('stderr', Buffer.from('\n'));
  parser.flush();
  assert.deepEqual(events, [
    { kind: 'package_summary', operation: 'resolving', count: 2, message: 'Resolved 2 Python packages.' },
    { kind: 'package_summary', operation: 'installing', count: 2, message: 'Prepared 2 Python packages.' },
    { kind: 'package_installed', operation: 'installing', artifact_name: 'torchaudio==2.8.0+cu128', message: 'Installed torchaudio==2.8.0+cu128.' },
    { kind: 'package_installed', operation: 'installing', artifact_name: 'torch==2.8.0+cu128', message: 'Installed torch==2.8.0+cu128.' },
  ]);
  assert.doesNotMatch(JSON.stringify(events), /\/home\/|https?:|token|stderr|stdout/);
});

test('uv output parser ignores terminal animations, arbitrary lines, URLs, and malformed package identities', () => {
  const events = [];
  const parser = new UvOutputProgress((event) => events.push(event), { minIntervalMs: 0 });
  parser.push('stderr', Buffer.from('Downloading torch 0%\rDownloading torch 50%\rInstalling from https://secret.invalid/pkg\n+ unsafe/name==1.0\n+ torch==2.8.0\n'));
  parser.flush();
  assert.deepEqual(events, [{ kind: 'package_installed', operation: 'installing', artifact_name: 'torch==2.8.0', message: 'Installed torch==2.8.0.' }]);
});

test('uv package output throttles bursts and emits the latest package when flushed', () => {
  let now = 100;
  const events = [];
  const parser = new UvOutputProgress((event) => events.push(event), { clock: () => now, minIntervalMs: 250 });
  parser.push('stderr', Buffer.from('+ numpy==2.0.0\n+ scipy==1.14.0\n'));
  assert.equal(events.length, 1);
  now = 200;
  parser.flush();
  assert.equal(events.length, 2);
  assert.equal(events[1].artifact_name, 'scipy==1.14.0');
});
