import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { RuntimeTasks } from '../src/runtime-tasks.js';

const sha = (value) => createHash('sha256').update(value).digest('hex');

function context(tasks, suffix = '00000000-0000-4000-8000-000000000001') {
  return { attemptId: suffix, signal: new AbortController().signal,
    componentStatus: tasks.initialBootstrapStatus(),
    onComponentStatus(value) { this.componentStatus = value; }, onProgress() {},
    async onChildProcess() {} };
}

test('managed FFmpeg installation verifies both pinned executables, reuses them, and repairs same-size corruption', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-ffmpeg-install-'));
  const ffmpeg = Buffer.from('#!/bin/sh\n# fixture ffmpeg\n');
  const ffprobe = Buffer.from('#!/bin/sh\n# fixture ffprobe\n');
  const archiveBytes = Buffer.from('tiny trusted archive fixture');
  let transfers = 0;
  const artifact = { available: true, version: 'fixture-1', version_identity: 'fixture-1', release: 'test fixture',
    source_revision: 'fixture-commit', url: 'https://github.com/example/ffmpeg/releases/download/fixture/archive.tar.xz',
    file: 'archive.tar.xz', size_bytes: archiveBytes.length, sha256: sha(archiveBytes), tar_root: 'ffmpeg-fixture',
    estimated_install_bytes: ffmpeg.length + ffprobe.length,
    executables: { ffmpeg: { size_bytes: ffmpeg.length, sha256: sha(ffmpeg) },
      ffprobe: { size_bytes: ffprobe.length, sha256: sha(ffprobe) } }, license: 'LGPL-3.0-or-later' };
  const runtimeRecipe = { ffmpeg: artifact, disk_space: { safety_margin_bytes: 0 } };
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, runtimeRecipe,
    fetchImpl: async () => {
      transfers += 1;
      return new Response(archiveBytes, { status: 200, headers: { 'content-length': String(archiveBytes.length) } });
    }, processRunner: async (executable, args) => {
      if (args[0] === '-I' && args[1] === '-c') {
        const target = args.at(-1);
        fs.writeFileSync(path.join(target, 'ffmpeg'), ffmpeg, { mode: 0o600, flag: 'wx' });
        fs.writeFileSync(path.join(target, 'ffprobe'), ffprobe, { mode: 0o600, flag: 'wx' });
        fs.writeFileSync(path.join(target, 'COPYING.LGPLv3.txt'), 'fixture license');
        return { code: 0, stdout: '', stderr: '' };
      }
      const name = path.basename(executable);
      return { code: 0, stdout: `${name} version fixture-1 Copyright\nconfiguration: --enable-version3 --disable-gpl --disable-nonfree\n`, stderr: '' };
    } });
  tasks.paths = Object.fromEntries(['home', 'temporary', 'uvCache', 'pythonRoot', 'hfHome', 'hfHub', 'hfXet', 'stagingRoot', 'quarantineRoot']
    .map((key) => [key, path.join(root, key)]));
  for (const value of Object.values(tasks.paths)) fs.mkdirSync(value, { recursive: true });
  const first = context(tasks);
  first.managedPython = '/managed/cpython';
  await tasks.installManagedFfmpeg(first);
  const installed = first.componentStatus.components.ffmpeg.path;
  assert.equal(first.componentStatus.components.ffmpeg.state, 'files_verified');
  assert.equal(transfers, 1);
  assert.equal(await tasks.verifyFfmpegDirectory(installed, first), true);

  const second = context(tasks, '00000000-0000-4000-8000-000000000002');
  await tasks.installManagedFfmpeg(second);
  assert.equal(transfers, 1, 'a verified installation is reused without a transfer');
  assert.equal(second.componentStatus.components.ffmpeg.path, installed);

  const executable = path.join(installed, 'ffmpeg');
  fs.writeFileSync(executable, Buffer.alloc(ffmpeg.length, 0x21));
  const repair = context(tasks, '00000000-0000-4000-8000-000000000003');
  repair.managedPython = '/managed/cpython';
  await tasks.installManagedFfmpeg(repair);
  assert.equal(transfers, 2, 'a same-size corrupt executable triggers a verified repair');
  assert.equal(repair.componentStatus.components.ffmpeg.state, 'files_verified');
  assert.equal(await tasks.verifyFfmpegDirectory(installed, repair), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('macOS selects the pinned source-build FFmpeg recipe and rejects a GPL configuration', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-ffmpeg-mac-'));
  const archiveBytes = Buffer.from('source archive');
  const artifact = {
    available: true, acquisition: 'pinned_source_build', platform: 'macos-arm64',
    version: 'n8.1.3-18-g46d899d7a0', version_identity: 'n8.1.3-18-g46d899d7a0',
    source_revision: '46d899d7a02bc63237472cc4897258d5ddf274ac',
    url: 'https://github.com/FFmpeg/FFmpeg/archive/46d899d7a02bc63237472cc4897258d5ddf274ac.tar.gz',
    file: 'source.tar.gz', size_bytes: archiveBytes.length, sha256: sha(archiveBytes),
    tar_root: 'FFmpeg-source', license: 'LGPL-2.1-or-later',
    configure: ['--disable-gpl', '--disable-nonfree'],
  };
  let prefix;
  const tasks = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, environment: { profile: 'macos-arm64' },
    runtimeRecipe: { ffmpeg_profiles: { 'macos-arm64': artifact }, disk_space: { safety_margin_bytes: 0 } },
    systemToolRunner: (tool, args) => ({ status: 0, stdout: tool === 'xcrun' && args.includes('--show-sdk-path') ? os.tmpdir() : '/Library/Developer/CommandLineTools', stderr: '' }),
    fetchImpl: async () => new Response(archiveBytes, { status: 200, headers: { 'content-length': String(archiveBytes.length) } }),
    processRunner: async (executable, args) => {
      if (String(executable).endsWith('configure')) {
        prefix = args.find((item) => item.startsWith('--prefix=')).slice('--prefix='.length);
        return { code: 0, stdout: '', stderr: '' };
      }
      if (executable === 'make' && args.at(-1) === 'install') {
        fs.mkdirSync(path.join(prefix, 'bin'), { recursive: true });
        for (const name of ['ffmpeg', 'ffprobe']) fs.writeFileSync(path.join(prefix, 'bin', name), `#!/bin/sh\nprintf '%s\\n' '${name} version n8.1.3-18-g46d899d7a0' 'configuration: --disable-gpl --disable-nonfree'\n`, { mode: 0o755 });
        return { code: 0, stdout: '', stderr: '' };
      }
      if (executable === 'make' || (args[0] === '-I' && args[1] === '-c')) return { code: 0, stdout: '', stderr: '' };
      const name = path.basename(executable);
      return { code: 0, stdout: fs.readFileSync(executable, 'utf8').includes('printf') ? `${name} version n8.1.3-18-g46d899d7a0\nconfiguration: --disable-gpl --disable-nonfree\n` : '', stderr: '' };
    } });
  tasks.paths = Object.fromEntries(['home', 'temporary', 'uvCache', 'pythonRoot', 'hfHome', 'hfHub', 'hfXet', 'stagingRoot', 'quarantineRoot']
    .map((key) => [key, path.join(root, key)]));
  for (const value of Object.values(tasks.paths)) fs.mkdirSync(value, { recursive: true });
  const first = context(tasks);
  first.managedPython = '/managed/cpython';
  await tasks.installSourceFfmpeg(first, artifact);
  assert.equal(first.componentStatus.components.ffmpeg.state, 'files_verified');
  assert.equal(first.componentStatus.components.ffmpeg.runtime_ready, false);
  const installed = first.componentStatus.components.ffmpeg.path;
  fs.writeFileSync(path.join(installed, 'ffmpeg'), Buffer.from('#!/bin/sh\nprintf "%s\\n" "ffmpeg version other"\n'));
  assert.equal(await tasks.verifyFfmpegDirectory(installed, first, artifact), false);
  const missingTools = new RuntimeTasks({ dataDirectory: root, applicationRoot: root, environment: { profile: 'macos-arm64' },
    runtimeRecipe: { ffmpeg_profiles: { 'macos-arm64': artifact } },
    systemToolRunner: () => ({ status: 1, stdout: '', stderr: 'missing' }) });
  await assert.rejects(missingTools.installSourceFfmpeg(context(missingTools), artifact),
    (error) => error.code === 'MACOS_COMMAND_LINE_TOOLS_REQUIRED' && /xcode-select --install/.test(error.message));
  const unavailable = new RuntimeTasks({ dataDirectory: root, applicationRoot: root,
    systemToolRunner: (tool) => tool === 'make' ? { error: new Error('ENOENT'), status: null } : { status: 0, stdout: os.tmpdir() } });
  assert.throws(() => unavailable.verifyMacFfmpegBuildTools(), /make executable/);
  const unusable = new RuntimeTasks({ dataDirectory: root, applicationRoot: root,
    systemToolRunner: (tool) => tool === 'make' ? { status: 2, stdout: '', stderr: 'bad makefile' } : { status: 0, stdout: os.tmpdir() } });
  assert.throws(() => unusable.verifyMacFfmpegBuildTools(), /usable make implementation/);
  const valid = new RuntimeTasks({ dataDirectory: root, applicationRoot: root,
    systemToolRunner: (tool, args) => ({ status: 0, stdout: tool === 'xcrun' && args.includes('--show-sdk-path') ? os.tmpdir() : '/usr/bin/tool' }) });
  assert.equal(valid.verifyMacFfmpegBuildTools(), true);
  fs.rmSync(root, { recursive: true, force: true });
});

test('Apple Silicon runner has a usable real FFmpeg build toolchain', { skip: process.platform !== 'darwin' }, () => {
  const tasks = new RuntimeTasks({ dataDirectory: os.tmpdir(), applicationRoot: process.cwd() });
  assert.equal(tasks.verifyMacFfmpegBuildTools(), true);
});
