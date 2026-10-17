#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { sha256File, validateWindowsPackage } from './windows-package-lib.mjs';

const GO_VERSION = '1.27.2';
const GO_ARCHIVE = `go${GO_VERSION}.linux-amd64.tar.gz`;
const GO_URL = `https://go.dev/dl/${GO_ARCHIVE}`;
const GO_SHA256 = 'ecbadb99091a3f46e31f5f934b068b1864eafa7995211b39eaddf76996045fe5';
const GO_SIZE = 70590635;

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const argv = process.argv.slice(2);
const valueAfter = (flag) => {
  const index = argv.indexOf(flag);
  return index < 0 ? null : argv[index + 1] || null;
};
const outputArg = valueAfter('--output');
const sourceRevisionArg = valueAfter('--source-revision');
const linuxArchiveArg = valueAfter('--linux-archive');
if (!outputArg || !/^[a-f0-9]{40}$/.test(sourceRevisionArg || '')) {
  throw new Error('Usage: node launcher/scripts/package-windows.mjs --source-revision 40-character-commit --output ABSOLUTE_OUTPUT_DIRECTORY [--linux-archive ABSOLUTE_LINUX_ARCHIVE]');
}
const outputRoot = path.resolve(outputArg);
if (!path.isAbsolute(outputArg) || outputRoot.startsWith(`${repositoryRoot}${path.sep}`)) {
  throw new Error('Choose an absolute output directory outside the source checkout.');
}
if (fs.existsSync(outputRoot)) throw new Error(`Output already exists: ${outputRoot}`);
if (process.arch !== 'x64' || process.platform !== 'linux') throw new Error('This Windows package is cross-compiled from Linux x86-64.');

const revision = spawnSync('git', ['rev-parse', `${sourceRevisionArg}^{commit}`], { cwd: repositoryRoot, encoding: 'utf8' });
if (revision.status !== 0 || revision.stdout.trim() !== sourceRevisionArg) throw new Error('The requested source revision did not resolve exactly.');
const dirty = spawnSync('git', ['status', '--porcelain', '--', 'launcher/windows', 'launcher/installer/install-windows.ps1', 'launcher/installer/install-linux.py', 'launcher/WINDOWS.md', 'launcher/windows/accept-windows.ps1'], { cwd: repositoryRoot, encoding: 'utf8' });
if (dirty.stdout.trim()) throw new Error('Windows packaging inputs are modified. Commit them before building.');

const version = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'frontend/package.json'), 'utf8')).version;
const appName = `speechcraft-${version}-windows-x64`;
fs.mkdirSync(path.dirname(outputRoot), { recursive: true, mode: 0o700 });
const work = fs.mkdtempSync(path.join(path.dirname(outputRoot), '.speechcraft-windows-'));
const stage = path.join(work, appName);
fs.mkdirSync(stage, { mode: 0o755 });

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: repositoryRoot, stdio: 'inherit', shell: false, ...options });
  if (result.error || result.status !== 0) throw new Error(`${command} ${args.join(' ')} failed.`);
}

async function acquireGo() {
  const destination = path.join(work, GO_ARCHIVE);
  const response = await fetch(GO_URL, { redirect: 'follow', signal: AbortSignal.timeout(180_000) });
  if (!response.ok) throw new Error(`Could not download the pinned Go toolchain (${response.status}).`);
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length !== GO_SIZE || crypto.createHash('sha256').update(bytes).digest('hex') !== GO_SHA256) {
    throw new Error('The Go toolchain failed its pinned size or SHA-256 check.');
  }
  fs.writeFileSync(destination, bytes, { mode: 0o600 });
  run('tar', ['-xzf', destination, '-C', work]);
  const go = path.join(work, 'go', 'bin', 'go');
  const versionResult = spawnSync(go, ['version'], { encoding: 'utf8', shell: false });
  if (versionResult.status !== 0 || !versionResult.stdout.startsWith(`go version go${GO_VERSION} `)) {
    throw new Error('The verified Go toolchain reported an unexpected version.');
  }
  return go;
}

try {
  const archive = path.join(work, 'source.tar');
  const archiveFd = fs.openSync(archive, 'w');
  const exported = spawnSync('git', ['archive', '--format=tar', sourceRevisionArg, '--', 'launcher/windows', 'launcher/installer/install-windows.ps1', 'launcher/installer/install-linux.py', 'launcher/WINDOWS.md', 'launcher/windows/accept-windows.ps1'], { cwd: repositoryRoot, stdio: ['ignore', archiveFd, 'inherit'], shell: false });
  fs.closeSync(archiveFd);
  if (exported.status !== 0) throw new Error('Could not export the pinned Windows source files.');
  const source = path.join(work, 'source');
  fs.mkdirSync(source);
  run('tar', ['-xf', archive, '-C', source]);
  const go = await acquireGo();
  const exe = path.join(stage, 'speechcraft.exe');
  const build = spawnSync(go, ['build', '-trimpath', '-ldflags', '-s -w -buildid=', '-o', exe, './cmd/speechcraft'], {
    cwd: path.join(source, 'launcher/windows'),
    env: { ...process.env, CGO_ENABLED: '0', GOOS: 'windows', GOARCH: 'amd64', GOFLAGS: '-mod=readonly' },
    stdio: 'inherit',
    shell: false,
  });
  if (build.status !== 0) throw new Error('Windows launcher cross-compile failed.');
  for (const [from, to] of [
    ['launcher/installer/install-windows.ps1', 'install-windows.ps1'],
    ['launcher/installer/install-linux.py', 'install-linux.py'],
    ['launcher/windows/installation.schema.json', 'installation.schema.json'],
    ['launcher/WINDOWS.md', 'WINDOWS.md'],
    ['launcher/windows/accept-windows.ps1', 'accept-windows.ps1'],
  ]) {
    const sourceFile = path.join(source, from);
    if (!fs.existsSync(sourceFile)) throw new Error(`Pinned source is missing ${from}.`);
    fs.copyFileSync(sourceFile, path.join(stage, to));
  }
  const linuxPayload = { status: 'external_verified_dependency', platform: 'linux-x86_64' };
  if (linuxArchiveArg) {
    const linuxArchive = path.resolve(linuxArchiveArg);
    for (const suffix of ['', '.sha256', '.release.json']) {
      if (!fs.existsSync(linuxArchive + suffix)) throw new Error(`Linux archive dependency is missing ${path.basename(linuxArchive + suffix)}.`);
      fs.copyFileSync(linuxArchive + suffix, path.join(stage, path.basename(linuxArchive + suffix)));
    }
    const release = JSON.parse(fs.readFileSync(`${linuxArchive}.release.json`, 'utf8'));
    if (release.platform !== 'linux-x86_64' || release.source_revision !== sourceRevisionArg || release.archive_sha256 !== sha256File(linuxArchive)) {
      throw new Error('The Linux archive does not match this Windows source revision.');
    }
    linuxPayload.status = 'included';
    linuxPayload.archive = path.basename(linuxArchive);
    linuxPayload.archive_sha256 = release.archive_sha256;
    linuxPayload.application_version = release.application_version;
  }
  const release = {
    schema_version: 1,
    application: 'SpeechCraft',
    version,
    platform: 'windows-x64',
    source_revision: sourceRevisionArg,
    build_platform: `${os.platform()}-${os.arch()}`,
    launcher: { language: 'go', target: 'windows/amd64', toolchain: `go${GO_VERSION}`, toolchain_sha256: GO_SHA256, file: 'speechcraft.exe', sha256: sha256File(exe) },
    linux_payload: linuxPayload,
    signing: { status: 'unsigned_development_build', smartscreen: 'not_established' },
    notices: [
      'This development build is not code-signed and has not established SmartScreen trust.',
      'Native Windows, WSL localhost networking, CUDA, model download, and dataset acceptance were not run by the package build.',
      'Public FFmpeg redistribution and TitaNet provenance remain unresolved.',
    ],
  };
  fs.writeFileSync(path.join(stage, 'windows-release.json'), `${JSON.stringify(release, null, 2)}\n`, { mode: 0o644 });
  const checked = validateWindowsPackage(stage);
  if (!checked.ok) throw new Error(`Windows package validation failed: ${(checked.problems || checked.missing).join(', ')}`);
  const bundle = path.join(work, `${appName}.tar.gz`);
  run('tar', ['--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner', '-czf', bundle, '-C', work, appName]);
  const bundleSha = sha256File(bundle);
  fs.mkdirSync(outputRoot, { recursive: true });
  fs.renameSync(bundle, path.join(outputRoot, path.basename(bundle)));
  fs.writeFileSync(path.join(outputRoot, `${path.basename(bundle)}.sha256`), `${bundleSha}  ${path.basename(bundle)}\n`, { mode: 0o644 });
  fs.copyFileSync(path.join(stage, 'windows-release.json'), path.join(outputRoot, 'windows-release.json'));
  const external = { ...release, archive: path.basename(bundle), archive_sha256: bundleSha, archive_size_bytes: fs.statSync(path.join(outputRoot, path.basename(bundle))).size };
  fs.writeFileSync(path.join(outputRoot, `${path.basename(bundle)}.release.json`), `${JSON.stringify(external, null, 2)}\n`, { mode: 0o644 });
  console.log(`Built ${path.join(outputRoot, path.basename(bundle))}`);
  console.log(`SHA-256 ${bundleSha}; unsigned development build; Linux payload ${linuxPayload.status}.`);
} catch (error) {
  fs.rmSync(outputRoot, { recursive: true, force: true });
  throw error;
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
