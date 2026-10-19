import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { runMacUninstall } from '../src/uninstall-macos.js';

const PATH_BLOCK = '# >>> SpeechCraft user command PATH >>>\ncase ":$PATH:" in *:"$HOME/Library/Application Support/SpeechCraft/bin:"*) ;; *) PATH="$HOME/Library/Application Support/SpeechCraft/bin:$PATH" ;; esac\nexport PATH\n# <<< SpeechCraft user command PATH <<<\n';
const WRAPPER = '#!/bin/sh\nexec "${HOME}/Library/Application Support/SpeechCraft/app/current/bin/speechcraft" "$@"\n';

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-macos-uninstall-'));
  const home = path.join(root, 'home'); fs.mkdirSync(home);
  const base = path.join(home, 'Library/Application Support/SpeechCraft');
  const app = path.join(base, 'app'); const releases = path.join(app, 'releases'); const bin = path.join(base, 'bin');
  const version = '0.1.1'; const release = path.join(releases, `speechcraft-${version}-macos-arm64`);
  fs.mkdirSync(release, { recursive: true }); fs.mkdirSync(bin, { recursive: true });
  fs.writeFileSync(path.join(release, 'application-release-manifest.json'), JSON.stringify({ schema_version: 1,
    application: 'SpeechCraft', platform: 'macos-arm64', version }));
  fs.symlinkSync(`releases/${path.basename(release)}`, path.join(app, 'current'));
  const command = path.join(bin, 'speechcraft'); fs.writeFileSync(command, WRAPPER, { mode: 0o755 });
  const managed = path.join(base, 'setup-controller'); fs.mkdirSync(path.join(managed, 'models'), { recursive: true });
  const model = path.join(managed, 'models', 'keep'); fs.writeFileSync(model, 'model');
  const zprofile = path.join(home, '.zprofile'); fs.writeFileSync(zprofile, `# user setup\n${PATH_BLOCK}# user tail\n`);
  return { root, home, base, app, releases, bin, release, managed, model, command, zprofile };
}

test('Mac uninstall removes only application files and keeps persistent data by default', async () => {
  const f = fixture(); let stopped = 0;
  try {
    const result = await runMacUninstall({ home: f.home, env: { SPEECHCRAFT_INSTALL_PLATFORM_TEST: 'darwin' }, yes: true,
      platform: 'darwin', stopServices: async (directory) => { stopped += 1; assert.equal(directory, path.join(f.base, 'setup-controller')); return { stopped: true }; } });
    assert.deepEqual(result, { removed: true, purged: false });
    assert.equal(stopped, 1);
    assert.equal(fs.existsSync(f.release), false);
    assert.equal(fs.existsSync(f.command), false);
    assert.equal(fs.readFileSync(f.model, 'utf8'), 'model');
    assert.match(fs.readFileSync(f.zprofile, 'utf8'), /# user setup\n# user tail/);
    assert.equal(fs.readFileSync(f.zprofile, 'utf8').includes('SpeechCraft user command PATH'), false);
  } finally { fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('Mac --purge removes only the known SpeechCraft managed-data directory', async () => {
  const f = fixture(); const outside = path.join(f.root, 'outside'); fs.writeFileSync(outside, 'keep');
  try {
    const result = await runMacUninstall({ home: f.home, env: { SPEECHCRAFT_INSTALL_PLATFORM_TEST: 'darwin' }, yes: true, purge: true,
      stopServices: async () => ({ stopped: true }) });
    assert.deepEqual(result, { removed: true, purged: true });
    assert.equal(fs.existsSync(f.managed), false);
    assert.equal(fs.readFileSync(outside, 'utf8'), 'keep');
  } finally { fs.rmSync(f.root, { recursive: true, force: true }); }
});

test('Mac uninstall refuses unsafe command wrappers and refuses removal when owned services cannot stop', async () => {
  const f = fixture();
  try {
    fs.writeFileSync(f.command, '#!/bin/sh\necho other tool\n');
    await assert.rejects(runMacUninstall({ home: f.home, env: { SPEECHCRAFT_INSTALL_PLATFORM_TEST: 'darwin' }, yes: true,
      stopServices: async () => ({ stopped: true }) }), /unrelated command/);
    assert.equal(fs.existsSync(f.release), true);
  } finally { fs.rmSync(f.root, { recursive: true, force: true }); }

  const g = fixture();
  try {
    await assert.rejects(runMacUninstall({ home: g.home, env: { SPEECHCRAFT_INSTALL_PLATFORM_TEST: 'darwin' }, yes: true,
      stopServices: async () => { throw new Error('ownership uncertain'); } }), /ownership uncertain/);
    assert.equal(fs.existsSync(g.release), true);
    assert.equal(fs.existsSync(g.command), true);
    assert.equal(fs.existsSync(g.model), true);
  } finally { fs.rmSync(g.root, { recursive: true, force: true }); }
});

test('Mac purge rejects arbitrary data paths before stopping or deleting anything', async () => {
  const f = fixture(); let stopped = 0;
  try {
    await assert.rejects(runMacUninstall({ home: f.home, env: { SPEECHCRAFT_INSTALL_PLATFORM_TEST: 'darwin' }, yes: true, purge: true,
      dataDirectory: path.join(f.root, 'outside'), stopServices: async () => { stopped += 1; } }), /limited to the default/);
    assert.equal(stopped, 0);
    assert.equal(fs.existsSync(f.release), true);
  } finally { fs.rmSync(f.root, { recursive: true, force: true }); }
});
