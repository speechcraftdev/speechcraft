import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { gzipSync } from 'node:zlib';
import test from 'node:test';
import { BootstrapError, BootstrapTasks, extractUvArchive, runOwnedProcess } from '../src/bootstrap.js';
import { SetupController } from '../src/controller.js';
import { StateStore } from '../src/state-store.js';

function hash(bytes) { return createHash('sha256').update(bytes).digest('hex'); }

function tarEntry(name, bytes = Buffer.alloc(0), type = '0') {
  const header = Buffer.alloc(512);
  header.write(name, 0, 100, 'utf8');
  header.write('0000700\0', 100, 8, 'ascii');
  header.write('0000000\0', 108, 8, 'ascii');
  header.write('0000000\0', 116, 8, 'ascii');
  header.write(`${bytes.length.toString(8).padStart(11, '0')}\0`, 124, 12, 'ascii');
  header.write('00000000000\0', 136, 12, 'ascii');
  header.fill(32, 148, 156);
  header.write(type, 156, 1, 'ascii');
  header.write('ustar\0', 257, 6, 'ascii');
  header.write('00', 263, 2, 'ascii');
  const checksum = header.reduce((sum, value) => sum + value, 0);
  header.write(`${checksum.toString(8).padStart(6, '0')}\0 `, 148, 8, 'ascii');
  const padding = Buffer.alloc((512 - (bytes.length % 512)) % 512);
  return Buffer.concat([header, bytes, padding]);
}

function archiveFor(files) {
  const entries = [tarEntry('fixture-uv/', Buffer.alloc(0), '5'), ...Object.entries(files).map(([name, value]) =>
    tarEntry(`fixture-uv/${name}`, Buffer.from(value), '0')), Buffer.alloc(1024)];
  return gzipSync(Buffer.concat(entries));
}

const pythonProgram = String.raw`#!/usr/bin/env node
const path = require('node:path');
const source = process.argv[process.argv.indexOf('-c') + 1] || '';
const executable = process.argv[1];
const runtime = process.env.UV_PYTHON_INSTALL_DIR;
if (source.includes('SPEECHCRAFT_PYTHON_PROBE')) {
  console.log(JSON.stringify({ kind: 'SPEECHCRAFT_PYTHON_PROBE', version: '3.12.15', machine: 'x86_64', system: 'Linux', executable, prefix: runtime, base_prefix: runtime }));
} else if (source.includes('SPEECHCRAFT_MODEL_MANAGER_PROBE')) {
  console.log(JSON.stringify({ kind: 'SPEECHCRAFT_MODEL_MANAGER_PROBE', version: '0.1.0', roles: ['asr', 'diarization', 'speaker_purity', 'speech_detection'], python: '3.12.15', heavy_dependencies: [], executable }));
} else if (process.argv.includes('--help')) {
  console.log('speechcraft_models install verify status');
} else {
  process.exitCode = 3;
}`;

const uvProgram = String.raw`#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
const py = ${JSON.stringify(pythonProgram)};
const executable = path.join(process.env.UV_PYTHON_INSTALL_DIR, 'bin', 'python');
if (args[0] === '--version') console.log('uv 0.0.1-test');
else if (args[0] === 'python' && args[1] === 'find') {
  if (fs.existsSync(executable)) console.log(executable); else process.exitCode = 1;
} else if (args[0] === 'python' && args[1] === 'install') {
  fs.mkdirSync(path.dirname(executable), { recursive: true }); fs.writeFileSync(executable, py, { mode: 0o755 }); fs.chmodSync(executable, 0o755);
} else if (args[0] === 'venv') {
  const target = args.at(-1); const envPython = path.join(target, 'bin', 'python');
  fs.mkdirSync(path.dirname(envPython), { recursive: true }); fs.writeFileSync(envPython, py, { mode: 0o755 }); fs.chmodSync(envPython, 0o755);
} else if (args[0] === 'pip' && ['sync', 'install'].includes(args[1])) { /* deterministic stand-in */ }
else process.exitCode = 2;`;

function testManifest(archive, uvBytes, requirementsBytes) {
  return {
    uv: { version: '0.0.1-test', artifacts: { 'linux-x86_64': {
      file: 'uv-test.tar.gz', url: null, size_bytes: archive.length, sha256: hash(archive),
      tar_root: 'fixture-uv', executable: 'uv', executable_size_bytes: uvBytes.length,
      executable_sha256: hash(uvBytes),
    } } },
    python: { version: '3.12.15', build: 'fixture', supported_profiles: { 'linux-x86_64': { sha256: 'a'.repeat(64), size_bytes: 1234 } } },
    model_manager: { version: '0.1.0', dependency_lock: 'requirements.txt', requirements_sha256: hash(requirementsBytes) },
    disk_space: { safety_margin_bytes: 1, estimated_python_environment_bytes: 1,
      estimated_model_manager_environment_and_cache_bytes: 1 },
  };
}

test('uv extraction accepts only the expected plain files and rejects archive links', () => {
  const uv = Buffer.from(uvProgram);
  const artifact = { tar_root: 'fixture-uv', executable_size_bytes: uv.length };
  assert.deepEqual(extractUvArchive(archiveFor({ uv, uvx: 'unused' }), artifact), uv);
  assert.throws(() => extractUvArchive(gzipSync(Buffer.concat([
    tarEntry('fixture-uv/', Buffer.alloc(0), '5'), tarEntry('fixture-uv/uv', uv), tarEntry('fixture-uv/uvx', Buffer.from('x'), '2'), Buffer.alloc(1024),
  ])), artifact), (error) => error.code === 'UV_ARCHIVE_UNSAFE');
});

test('real Node bootstrap orchestration installs and reuses uv, managed Python, and the isolated model manager', { skip: process.platform !== 'linux' || process.arch !== 'x64' }, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-bootstrap-integration-'));
  const uvBytes = Buffer.from(uvProgram);
  const archive = archiveFor({ uv: uvBytes, uvx: Buffer.from('stand-in uvx') });
  const server = http.createServer((_request, response) => {
    server.downloads += 1;
    response.writeHead(200, { 'content-length': archive.length, 'content-type': 'application/gzip' });
    response.end(archive);
  });
  server.downloads = 0;
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const profile = 'linux-x86_64';
  const requirementsBytes = Buffer.from('# tiny offline fixture\n');
  const manifest = testManifest(archive, uvBytes, requirementsBytes);
  manifest.uv.artifacts[profile].url = `http://127.0.0.1:${server.address().port}/uv`;
  const wheel = path.join(root, 'speechcraft_model_manager-0.1.0-py3-none-any.whl');
  fs.writeFileSync(wheel, 'offline test wheel');
  const requirements = path.join(root, 'requirements.txt');
  fs.writeFileSync(requirements, requirementsBytes);
  const tasks = new BootstrapTasks({ dataDirectory: root, wheelPath: wheel, wheelSha256: hash(fs.readFileSync(wheel)),
    bootstrapManifest: manifest, requirementsPath: requirements, availableBytes: 2 * 1024 ** 3 });
  const store = StateStore.open(root);
  const controller = new SetupController(store, { tasks, mode: 'bootstrap' });
  try {
    controller.start();
    await controller.waitForIdle();
    let job = store.snapshot().job;
    assert.equal(job.state, 'completed', JSON.stringify(job));
    assert.equal(job.bootstrap_status.uv_verified, true);
    assert.equal(job.bootstrap_status.python_verified, true);
    assert.equal(job.bootstrap_status.model_manager_verified, true);
    assert.equal(job.bootstrap_status.bootstrap_ready, true);
    assert.equal(job.bootstrap_status.production_ready, false);
    assert.equal(server.downloads, 1);
    const installedPython = job.bootstrap_status.python_executable;
    assert.ok(fs.existsSync(installedPython));

    tasks.availableBytesOverride = 1;
    controller.start();
    await controller.waitForIdle();
    job = store.snapshot().job;
    assert.equal(job.state, 'completed', JSON.stringify(job.error));
    assert.equal(job.bootstrap_status.bootstrap_ready, true);
    assert.equal(job.bootstrap_status.production_ready, false);
    assert.equal(server.downloads, 1, 'the verified uv archive must be reused');
  } finally {
    await new Promise((resolve) => server.close(resolve));
    store.releaseOwnership();
  }
});

test('bootstrap checks capacity before acquisition but allows reuse below the install estimate', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-bootstrap-disk-'));
  const archive = Buffer.from('archive fixture');
  const uvBytes = Buffer.from('binary fixture');
  const requirementsBytes = Buffer.from('');
  const manifest = testManifest(archive, uvBytes, requirementsBytes);
  const tasks = new BootstrapTasks({ dataDirectory: root,
    wheelPath: path.join(root, 'speechcraft_model_manager-0.1.0-py3-none-any.whl'),
    wheelSha256: 'a'.repeat(64), bootstrapManifest: manifest, requirementsPath: path.join(root, 'requirements.txt'),
    availableBytes: 2 * 1024 ** 3 });
  fs.writeFileSync(tasks.requirementsPath, requirementsBytes);
  const context = { attemptId: '00000000-0000-4000-8000-000000000000',
    signal: new AbortController().signal, onProgress() {}, onComponentStatus() {} };
  await tasks.run('inspect_system', context);
  tasks.availableBytesOverride = 1;
  await assert.rejects(tasks.run('install_uv', context),
  (error) => error.code === 'INSUFFICIENT_DISK_SPACE');
});

test('an owned bootstrap subprocess that ignores SIGTERM is killed and reaped before cancellation settles', { skip: process.platform === 'win32' }, async () => {
  const abort = new AbortController();
  const started = Date.now();
  const promise = runOwnedProcess(process.execPath, ['-e',
    "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000);"],
  { signal: abort.signal, cwd: os.tmpdir(), env: { PATH: process.env.PATH }, graceMs: 80 });
  setTimeout(() => abort.abort(), 50);
  await assert.rejects(promise, (error) => error instanceof BootstrapError && error.code === 'BOOTSTRAP_CANCELLED');
  assert.ok(Date.now() - started >= 50);
  assert.ok(Date.now() - started < 2_000);
});
