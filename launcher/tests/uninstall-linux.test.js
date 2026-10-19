import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import { stopForUninstall } from '../src/launcher.js';
import { runLinuxUninstall, UninstallError } from '../src/uninstall-linux.js';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const installer = path.join(repo, 'launcher/installer/install-linux.py');
const cli = path.join(repo, 'launcher/src/cli.js');

const FIXTURE = String.raw`
import hashlib, importlib.util, io, json, pathlib, sys, tarfile
installer, version = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("speechcraft_install_linux", installer)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
def sha(value):
    return hashlib.sha256(value).hexdigest()
runtime = {"worker": {"validation_helper": "workers/dataset/scripts/preflight.py"}, "runtime_validation": {"probes": [{"helper": "workers/dataset/scripts/runtime_probe.py"}], "fixtures": {"speech": {"path": "workers/dataset/runtime-fixtures/speech.wav"}}}}
package = {"runtime/node": (b"#!/bin/sh\nprintf 'v24.22.0\\n'\n", 0o755), "bin/speechcraft": (b"#!/bin/sh\necho speechcraft-fixture\n", 0o755), "setup-controller/resources/runtime-manifest.json": (json.dumps(runtime).encode(), 0o644)}
for relative in mod.MANDATORY_RUNTIME_FILES:
    package.setdefault(relative, (b"fixture-resource", 0o644))
records = [{"path": name, "size_bytes": len(content), "sha256": sha(content), "executable": bool(mode & 0o111)} for name, (content, mode) in package.items()]
internal = {"schema_version": 1, "application": "SpeechCraft", "version": version, "platform": "linux-x86_64", "node": {"version": "24.22.0", "executable": "runtime/node", "sha256": sha(package["runtime/node"][0])}, "runtime_inventory": {"status": "complete", "required_files": sorted(mod.MANDATORY_RUNTIME_FILES)}, "package_files": records}
internal_bytes = (json.dumps(internal, sort_keys=True) + "\n").encode()
app_name = f"speechcraft-{version}-linux-x86_64"
root = pathlib.Path(sys.argv[3])
archive = root / f"{app_name}.tar.gz"
members = {f"{app_name}/{name}": value for name, value in package.items()}
members[f"{app_name}/application-release-manifest.json"] = (internal_bytes, 0o644)
with tarfile.open(archive, "w:gz") as output:
    for name, (content, mode) in members.items():
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = mode
        output.addfile(info, io.BytesIO(content))
digest = sha(archive.read_bytes())
archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
external = {"schema_version": 1, "archive": archive.name, "archive_size_bytes": archive.stat().st_size, "archive_sha256": digest, "platform": "linux-x86_64", "application_version": version, "node_version": "24.22.0", "application_manifest_sha256": sha(internal_bytes)}
archive.with_name(archive.name + ".release.json").write_text(json.dumps(external), encoding="utf8")
mod.main(str(archive))
`;

function capture() {
  const chunks = [];
  return { write(value) { chunks.push(String(value)); }, text() { return chunks.join(''); } };
}

function makeHome() {
  return fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-uninstall-')));
}

function removeHome(home) {
  const visit = (target) => {
    const info = fs.lstatSync(target);
    if (info.isSymbolicLink()) return;
    if (info.isDirectory()) {
      fs.chmodSync(target, 0o700);
      for (const name of fs.readdirSync(target)) visit(path.join(target, name));
      return;
    }
    if (info.isFile()) fs.chmodSync(target, info.mode | 0o200);
  };
  visit(home);
  fs.rmSync(home, { recursive: true, force: true });
}

function locations(home) {
  const share = path.join(home, '.local/share/speechcraft');
  const app = path.join(share, 'app');
  return {
    share, app,
    command: path.join(home, '.local/bin/speechcraft'),
    current: path.join(app, 'current'),
    releases: path.join(app, 'releases'),
    release: (version = '0.1.0') => path.join(app, 'releases', `speechcraft-${version}-linux-x86_64`),
    data: path.join(share, 'data'),
    controller: path.join(share, 'setup-controller'),
    models: path.join(share, 'models'),
    profile: path.join(home, '.profile'),
  };
}

function installRelease(home, version = '0.1.0') {
  const result = spawnSync('python3', ['-c', FIXTURE, installer, version, home], {
    env: { ...process.env, HOME: home, PATH: '/usr/bin:/bin' },
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr || result.stdout);
}

function seedData(home) {
  const place = locations(home);
  fs.mkdirSync(place.data, { recursive: true, mode: 0o700 });
  fs.mkdirSync(place.controller, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(place.data, 'project.txt'), 'project-audio');
  fs.writeFileSync(path.join(place.controller, 'state.txt'), 'controller-state');
  return place;
}

async function uninstall(home, options = {}) {
  const stdout = capture();
  const result = await runLinuxUninstall({
    home,
    env: options.env || {},
    yes: options.yes !== false,
    purge: options.purge === true,
    platform: options.platform || 'linux',
    stdout,
    cwd: options.cwd || path.join(home, 'elsewhere'),
    invocationRoot: options.invocationRoot || path.join(home, 'source-checkout'),
    dataDirectory: options.dataDirectory || null,
    isTTY: options.isTTY === true,
    confirm: options.confirm,
    stopServices: options.stopServices || (async () => ({ stopped: true })),
    lockTimeoutMs: options.lockTimeoutMs,
    hooks: options.hooks,
    wsl: false,
  });
  return { result, text: stdout.text() };
}

test('linux uninstall from another directory removes only the per-user application', async () => {
  const home = makeHome();
  const cwd = path.join(home, 'Projects', 'notes');
  fs.mkdirSync(cwd, { recursive: true });
  fs.writeFileSync(path.join(cwd, 'keep.txt'), 'cwd-file');
  fs.mkdirSync(path.join(home, 'Projects/speechcraft'), { recursive: true });
  fs.writeFileSync(path.join(home, 'Projects/speechcraft/keep.txt'), 'not-the-install');
  installRelease(home);
  const place = seedData(home);
  fs.mkdirSync(path.join(home, '.local/bin'), { recursive: true });
  fs.writeFileSync(path.join(home, '.local/bin/other-tool'), 'leave-me');
  fs.writeFileSync(path.join(home, '.local/share/other-app'), 'leave-share');
  assert.equal(fs.statSync(place.release()).mode & 0o777, 0o555);
  const profileBefore = fs.readFileSync(place.profile, 'utf8');
  assert.match(profileBefore, /# >>> SpeechCraft user command PATH >>>/);
  let stoppedWhileInstalled = false;
  const { text } = await uninstall(home, {
    cwd,
    yes: true,
    stopServices: async () => {
      stoppedWhileInstalled = fs.existsSync(place.command) && fs.existsSync(place.release());
      return { stopped: true, status: 'not_running' };
    },
  });
  assert.equal(stoppedWhileInstalled, true);
  assert.equal(text, [
    'SpeechCraft uninstall plan:',
    `Command: ${place.command}`,
    `Application root: ${place.app}`,
    `Active release: ${place.release()}`,
    'Releases to remove:',
    `- ${place.release()}`,
    'Preserved data:',
    `- ${place.data}`,
    `- ${place.controller}`,
    `PATH block: removing the exact SpeechCraft block from ${place.profile}`,
    'This removes the application command and versioned releases. Project audio, datasets, exports, models, and controller state stay in place.',
    'Confirmed with --yes.',
    'SpeechCraft application uninstalled.',
    `Removed: ${place.command}`,
    `Removed: ${place.current}`,
    `Removed: ${place.release()}`,
    `Removed: ${place.releases}`,
    `Removed: ${place.app}`,
    'Preserved data:',
    `- ${place.data}`,
    `- ${place.controller}`,
    `PATH block: removed from ${place.profile}`,
    '',
  ].join('\n'));
  assert.equal(fs.existsSync(place.command), false);
  assert.equal(fs.existsSync(place.current), false);
  assert.equal(fs.existsSync(place.app), false);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  assert.equal(fs.readFileSync(path.join(place.controller, 'state.txt'), 'utf8'), 'controller-state');
  assert.equal(fs.readFileSync(path.join(cwd, 'keep.txt'), 'utf8'), 'cwd-file');
  assert.equal(fs.readFileSync(path.join(home, 'Projects/speechcraft/keep.txt'), 'utf8'), 'not-the-install');
  assert.equal(fs.readFileSync(path.join(home, '.local/bin/other-tool'), 'utf8'), 'leave-me');
  assert.equal(fs.readFileSync(path.join(home, '.local/share/other-app'), 'utf8'), 'leave-share');
  assert.equal(fs.existsSync(place.profile), true);
  assert.doesNotMatch(fs.readFileSync(place.profile, 'utf8'), /SpeechCraft user command PATH/);
  const again = await uninstall(home, { cwd, yes: true });
  assert.match(again.text, /SpeechCraft is not installed for this user/);
  installRelease(home, '0.2.0');
  assert.equal(fs.readlinkSync(place.current), 'releases/speechcraft-0.2.0-linux-x86_64');
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  removeHome(home);
});

test('noninteractive uninstall without --yes removes nothing', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  await assert.rejects(uninstall(home, { yes: false, isTTY: false }), /without --yes/);
  assert.equal(fs.existsSync(place.command), true);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  const cancelled = await uninstall(home, { yes: false, confirm: () => false });
  assert.match(cancelled.text, /Uninstall cancelled/);
  assert.equal(fs.existsSync(place.current), true);
  removeHome(home);
});

test('shutdown failure and an unsafe ownership record abort before deletion', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  await assert.rejects(uninstall(home, {
    stopServices: async () => { throw new Error('process identity is uncertain'); },
  }), /Uninstall aborted; no application files were removed/);
  assert.equal(fs.existsSync(place.release()), true);
  fs.writeFileSync(path.join(place.controller, 'launcher.processes.json'), '{}\n', { mode: 0o644 });
  await assert.rejects(uninstall(home, { stopServices: stopForUninstall }), /no process was stopped/);
  assert.equal(fs.readFileSync(place.command, 'utf8').includes('SpeechCraft per-user command wrapper'), true);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  removeHome(home);
});

test('purge deletes only verified managed directories and refuses unsafe targets', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  fs.mkdirSync(place.models, { mode: 0o700 });
  fs.writeFileSync(path.join(place.models, 'model.bin'), 'weights');
  const custom = path.join(place.share, 'custom-projects');
  fs.mkdirSync(custom, { mode: 0o700 });
  fs.writeFileSync(path.join(custom, 'keep.txt'), 'custom');
  const outside = path.join(home, 'outside-cache');
  fs.mkdirSync(outside, { mode: 0o700 });
  fs.writeFileSync(path.join(outside, 'keep.txt'), 'cache');
  fs.symlinkSync(path.join(outside, 'keep.txt'), path.join(place.data, 'alias'));
  const { text } = await uninstall(home, { purge: true, yes: true });
  assert.match(text, /Data to permanently delete:/);
  assert.match(text, /cannot be undone/);
  assert.match(text, new RegExp(`Still preserved:\\n- ${custom}`));
  assert.equal(fs.existsSync(place.data), false);
  assert.equal(fs.existsSync(place.controller), false);
  assert.equal(fs.existsSync(place.models), false);
  assert.equal(fs.readFileSync(path.join(custom, 'keep.txt'), 'utf8'), 'custom');
  assert.equal(fs.readFileSync(path.join(outside, 'keep.txt'), 'utf8'), 'cache');
  assert.equal(fs.existsSync(place.app), false);

  installRelease(home);
  const linked = path.join(home, 'linked-data');
  fs.mkdirSync(linked, { mode: 0o700 });
  fs.writeFileSync(path.join(linked, 'keep.txt'), 'linked');
  fs.symlinkSync(linked, place.data);
  fs.mkdirSync(place.controller, { mode: 0o700 });
  const purgedLink = await uninstall(home, { purge: true, yes: true });
  assert.match(purgedLink.text, /Left in place:/);
  assert.equal(fs.readFileSync(path.join(linked, 'keep.txt'), 'utf8'), 'linked');
  assert.equal(fs.lstatSync(place.data).isSymbolicLink(), true);

  installRelease(home);
  const xdg = path.join(home, 'xdg');
  fs.mkdirSync(path.join(xdg, 'speechcraft'), { recursive: true });
  fs.writeFileSync(path.join(xdg, 'speechcraft/keep.txt'), 'xdg');
  await assert.rejects(uninstall(home, { purge: true, yes: true, env: { XDG_DATA_HOME: xdg } }), /outside/);
  assert.equal(fs.existsSync(locations(home).command), true);
  assert.equal(fs.readFileSync(path.join(xdg, 'speechcraft/keep.txt'), 'utf8'), 'xdg');
  const external = path.join(home, 'external-data');
  fs.mkdirSync(external, { mode: 0o700 });
  fs.writeFileSync(path.join(external, 'keep.txt'), 'external');
  await assert.rejects(uninstall(home, { purge: true, yes: true, dataDirectory: external }), /outside/);
  assert.equal(fs.readFileSync(path.join(external, 'keep.txt'), 'utf8'), 'external');
  assert.equal(fs.existsSync(locations(home).release()), true);
  removeHome(home);
});

test('foreign wrappers, escaping pointers, and symlinked destinations are left untouched', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  const outside = path.join(home, 'outside-release');
  fs.mkdirSync(outside, { mode: 0o700 });
  fs.writeFileSync(path.join(outside, 'keep.txt'), 'outside');
  fs.chmodSync(place.releases, 0o700);
  fs.unlinkSync(place.current);
  fs.symlinkSync(outside, place.current);
  await assert.rejects(uninstall(home), /escapes the versioned release directory/);
  assert.equal(fs.readFileSync(path.join(outside, 'keep.txt'), 'utf8'), 'outside');
  assert.equal(fs.existsSync(place.release()), true);

  fs.unlinkSync(place.current);
  fs.symlinkSync('releases/speechcraft-0.1.0-linux-x86_64', place.current);
  fs.writeFileSync(place.command, '#!/bin/sh\necho foreign\n');
  await assert.rejects(uninstall(home), /unrelated command/);
  assert.match(fs.readFileSync(place.command, 'utf8'), /foreign/);
  assert.equal(fs.existsSync(place.release()), true);

  fs.unlinkSync(place.command);
  fs.symlinkSync('/usr/bin/true', place.command);
  await assert.rejects(uninstall(home), /unrelated command/);
  assert.equal(fs.readlinkSync(place.command), '/usr/bin/true');

  fs.unlinkSync(place.command);
  fs.unlinkSync(place.current);
  fs.symlinkSync(`releases/${path.basename(place.release())}`, place.current);
  fs.writeFileSync(place.command, `#!/bin/sh\n# SpeechCraft per-user command wrapper\nexec "${place.app}/current/bin/speechcraft" "$@"\n`);
  fs.chmodSync(place.command, 0o755);
  fs.chmodSync(place.release(), 0o700);
  const manifest = path.join(place.release(), 'application-release-manifest.json');
  fs.chmodSync(manifest, 0o644);
  fs.writeFileSync(manifest, '{"schema_version":1,"application":"Other"}\n');
  await assert.rejects(uninstall(home), /does not name a verified SpeechCraft release/);
  assert.equal(fs.existsSync(manifest), true);

  const linkedHome = makeHome();
  const target = path.join(linkedHome, 'elsewhere');
  fs.mkdirSync(path.join(target, 'bin'), { recursive: true });
  fs.writeFileSync(path.join(target, 'bin/speechcraft'), 'keep');
  fs.symlinkSync('elsewhere', path.join(linkedHome, '.local'));
  await assert.rejects(uninstall(linkedHome), /not a real directory/);
  assert.equal(fs.readFileSync(path.join(target, 'bin/speechcraft'), 'utf8'), 'keep');
  removeHome(home);
  removeHome(linkedHome);
});

test('a source checkout and an unknown prefix are not removed', async () => {
  const home = makeHome();
  const checkout = path.join(home, 'src', 'speechcraft');
  fs.mkdirSync(checkout, { recursive: true });
  fs.writeFileSync(path.join(checkout, 'package.json'), '{"name":"speechcraft"}\n');
  const absent = await uninstall(home, { invocationRoot: checkout, yes: true });
  assert.match(absent.text, /not installed/);
  assert.equal(fs.readFileSync(path.join(checkout, 'package.json'), 'utf8').includes('speechcraft'), true);
  installRelease(home);
  seedData(home);
  const prefix = path.join(home, 'opt', 'speechcraft');
  fs.mkdirSync(prefix, { recursive: true });
  fs.writeFileSync(path.join(prefix, 'marker'), 'prefix');
  await uninstall(home, { invocationRoot: prefix, yes: true });
  assert.equal(fs.readFileSync(path.join(prefix, 'marker'), 'utf8'), 'prefix');
  assert.equal(fs.readFileSync(path.join(checkout, 'package.json'), 'utf8').includes('speechcraft'), true);
  assert.equal(fs.existsSync(locations(home).app), false);
  removeHome(home);
});

test('profile edits and symlink profiles are preserved', async () => {
  const home = makeHome();
  installRelease(home);
  const place = locations(home);
  const original = fs.readFileSync(place.profile, 'utf8');
  fs.writeFileSync(place.profile, `# user setting\nexport FOO=1\n${original}`);
  await uninstall(home, { yes: true });
  const kept = fs.readFileSync(place.profile, 'utf8');
  assert.match(kept, /# user setting\nexport FOO=1\n/);
  assert.doesNotMatch(kept, /SpeechCraft user command PATH/);

  installRelease(home);
  const edited = fs.readFileSync(place.profile, 'utf8').replace('export PATH', 'export PATH # user-edit');
  fs.writeFileSync(place.profile, edited);
  await uninstall(home, { yes: true });
  assert.match(fs.readFileSync(place.profile, 'utf8'), /user-edit/);

  installRelease(home);
  const target = path.join(home, 'profile-target');
  fs.copyFileSync(place.profile, target);
  fs.unlinkSync(place.profile);
  fs.symlinkSync('profile-target', place.profile);
  await uninstall(home, { yes: true });
  assert.equal(fs.lstatSync(place.profile).isSymbolicLink(), true);
  assert.match(fs.readFileSync(target, 'utf8'), /SpeechCraft user command PATH/);
  removeHome(home);
});

test('a busy install lock fails closed and a dead lock can be retried', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  const lock = path.join(place.share, '.speechcraft-install.lock');
  fs.mkdirSync(lock, { mode: 0o700 });
  const holder = spawn('sleep', ['30'], { stdio: 'ignore' });
  fs.writeFileSync(path.join(lock, 'pid'), `${holder.pid}\n`);
  try {
    await assert.rejects(uninstall(home, { lockTimeoutMs: 200 }), /install lock/);
    assert.equal(fs.existsSync(place.command), true);
  } finally {
    holder.kill('SIGKILL');
    await once(holder, 'exit');
  }
  const retried = await uninstall(home, { lockTimeoutMs: 2_000 });
  assert.match(retried.text, /SpeechCraft application uninstalled/);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  removeHome(home);
});

test('an interrupted release removal stays retryable without touching user data', async () => {
  const home = makeHome();
  installRelease(home, '0.1.0');
  installRelease(home, '0.2.0');
  const place = seedData(home);
  await assert.rejects(uninstall(home, {
    hooks: { beforeReleaseRemoval() { throw new Error('injected release failure'); } },
  }), /injected release failure/);
  assert.equal(fs.existsSync(place.command), true);
  assert.equal(fs.existsSync(place.current), false);
  assert.equal(fs.existsSync(place.release('0.1.0')), true);
  assert.equal(fs.existsSync(place.release('0.2.0')), true);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  const retried = await uninstall(home);
  assert.match(retried.text, /SpeechCraft application uninstalled/);
  assert.equal(fs.existsSync(place.release('0.1.0')), false);
  assert.equal(fs.existsSync(place.release('0.2.0')), false);
  assert.equal(fs.readFileSync(path.join(place.controller, 'state.txt'), 'utf8'), 'controller-state');
  removeHome(home);
});

test('an installed copy can delete its own release directory', async () => {
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  fs.chmodSync(place.release(), 0o755);
  const script = path.join(place.release(), 'self-delete.mjs');
  const moduleUrl = new URL('../src/uninstall-linux.js', import.meta.url);
  fs.writeFileSync(script, `import { runLinuxUninstall } from ${JSON.stringify(moduleUrl.href)};
const chunks = [];
await runLinuxUninstall({
  home: ${JSON.stringify(home)},
  env: {},
  yes: true,
  platform: 'linux',
  cwd: ${JSON.stringify(place.release())},
  invocationRoot: ${JSON.stringify(place.release())},
  stdout: { write(value) { chunks.push(String(value)); } },
  stopServices: async () => ({ stopped: true }),
});
if (chunks.join('').includes('SpeechCraft application uninstalled.') !== true) process.exit(2);
`);
  const child = spawn(process.execPath, [script], { cwd: place.release(), stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
  const [code] = await once(child, 'exit');
  assert.equal(code, 0, stderr);
  assert.equal(fs.existsSync(script), false);
  assert.equal(fs.existsSync(place.app), false);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  removeHome(home);
});

test('the cli help lists uninstall and a noninteractive cli refuses without --yes', async () => {
  const help = spawnSync(process.execPath, [cli, '--help'], { encoding: 'utf8' });
  assert.equal(help.status, 0);
  assert.match(help.stdout, /speechcraft uninstall \[--yes\|--force\] \[--purge\|--remove-data\]/);
  const home = makeHome();
  installRelease(home);
  const place = seedData(home);
  const elsewhere = path.join(home, 'elsewhere');
  fs.mkdirSync(elsewhere);
  const child = spawn(process.execPath, [cli, 'uninstall'], {
    cwd: elsewhere,
    env: { ...process.env, HOME: home, SPEECHCRAFT_SETUP_DATA_DIR: '', XDG_DATA_HOME: '' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  child.stdout.setEncoding('utf8').on('data', (chunk) => { stdout += chunk; });
  child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
  const [code] = await once(child, 'exit');
  assert.equal(code, 1);
  assert.match(stdout, /SpeechCraft uninstall plan:/);
  assert.match(stderr, /without --yes/);
  assert.equal(fs.existsSync(place.command), true);
  assert.equal(fs.readFileSync(path.join(place.data, 'project.txt'), 'utf8'), 'project-audio');
  await assert.rejects(runLinuxUninstall({ platform: 'darwin', home, yes: true, stdout: capture(), stopServices: async () => ({ stopped: true }) }), UninstallError);
  removeHome(home);
});
