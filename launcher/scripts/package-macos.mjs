#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { REQUIRED_RUNTIME_FILES, validateRuntimeInventory } from '../src/package-inventory.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const argv = process.argv.slice(2);
const valueAfter = (flag) => { const index = argv.indexOf(flag); return index < 0 ? null : argv[index + 1] || null; };

export const MAC_REQUIRED_FILES = Object.freeze([
  ...REQUIRED_RUNTIME_FILES,
  'workers/dataset/runtime-profiles/macos-arm64/pyproject.toml',
  'workers/dataset/runtime-profiles/macos-arm64/uv.lock',
  'bin/speechcraft',
  'runtime/node',
]);

export function assertDarwinArm64(platform = process.platform, arch = process.arch) {
  if (platform !== 'darwin' || arch !== 'arm64') {
    throw new Error(`package-macos.mjs requires macOS Apple Silicon to execute and verify the bundled Darwin Node runtime. This host is ${platform}/${arch}.`);
  }
}

export function validateMacRelease(directory) {
  const missing = MAC_REQUIRED_FILES.filter((name) => !fs.existsSync(path.join(directory, name)));
  const releasePath = path.join(directory, 'application-release-manifest.json');
  if (!fs.existsSync(releasePath)) missing.push('application-release-manifest.json');
  if (missing.length) return { ok: false, missing };
  const release = JSON.parse(fs.readFileSync(releasePath, 'utf8'));
  const problems = [];
  if (release.platform !== 'macos-arm64') problems.push('platform');
  if (!/^[a-f0-9]{40}$/.test(release.source_revision || '')) problems.push('source_revision');
  if (release.signing?.notarized === true) problems.push('false notarization claim');
  const node = path.join(directory, 'runtime/node');
  const header = fs.readFileSync(node).subarray(0, 8);
  const machO = header[0] === 0xcf && header[1] === 0xfa && header[2] === 0xed && header[3] === 0xfe;
  if (!machO) problems.push('bundled node is not a 64-bit Mach-O executable');
  return { ok: problems.length === 0, missing, problems, release };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (!isMain) {
  // imported by tests
} else {
  const outputArg = valueAfter('--output');
  const sourceRevisionArg = valueAfter('--source-revision');
  if (!outputArg || !/^[a-f0-9]{40}$/.test(sourceRevisionArg || '')) {
    throw new Error('Usage: node launcher/scripts/package-macos.mjs --source-revision 40-character-commit --output ABSOLUTE_OUTPUT_DIRECTORY');
  }
  assertDarwinArm64;
  const cross = process.platform !== 'darwin' || process.arch !== 'arm64';
  if (cross) console.error(`Cross-building a macOS ARM64 archive from ${process.platform}/${process.arch}. The Darwin Node binary will be verified by its Mach-O header and will not be executed here.`);
  const revision = spawnSync('git', ['rev-parse', `${sourceRevisionArg}^{commit}`], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
  if (revision.status !== 0 || revision.stdout.trim() !== sourceRevisionArg) throw new Error('The requested source revision did not resolve exactly.');
  const versionText = spawnSync('git', ['show', `${sourceRevisionArg}:frontend/package.json`], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
  if (versionText.status !== 0) throw new Error('Could not read the pinned frontend version.');
  const version = JSON.parse(versionText.stdout).version;
  const outputRoot = path.resolve(outputArg);
  if (fs.existsSync(outputRoot)) throw new Error(`Output already exists: ${outputRoot}`);
  const work = fs.mkdtempSync(path.join(path.dirname(outputRoot), '.speechcraft-macos-'));
  const appName = `speechcraft-${version}-macos-arm64`;
  const appRoot = path.join(work, appName);
  fs.mkdirSync(appRoot, { recursive: true });
  const archive = path.join(work, 'source.tar');
  const archiveFd = fs.openSync(archive, 'w');
  const exported = spawnSync('git', ['archive', '--format=tar', sourceRevisionArg, '--', 'bin', 'frontend', 'launcher', 'setup-controller/src', 'setup-controller/resources', 'backend/app', 'backend/pyproject.toml', 'backend/uv.lock', 'workers/dataset', 'packages/model_manager', 'LICENSE'], { cwd: repositoryRoot, shell: false, stdio: ['ignore', archiveFd, 'inherit'] });
  fs.closeSync(archiveFd);
  if (exported.status !== 0) throw new Error(`Could not export pinned source revision ${sourceRevisionArg}.`);
  const source = path.join(work, 'source');
  fs.mkdirSync(source);
  spawnSync('tar', ['-xf', archive, '-C', source], { shell: false, stdio: 'inherit' });
  const installedNext = path.join(repositoryRoot, 'frontend/node_modules/next/package.json');
  if (!fs.existsSync(installedNext)) throw new Error('Frontend build dependencies are missing. Install them from frontend/bun.lock before packaging.');
  const expectedNext = JSON.parse(fs.readFileSync(path.join(source, 'frontend/package.json'), 'utf8')).dependencies?.next;
  const actualNext = JSON.parse(fs.readFileSync(installedNext, 'utf8')).version;
  if (expectedNext !== actualNext) throw new Error(`Pinned source requires Next.js ${expectedNext}, but the prepared dependency tree has ${actualNext}.`);
  spawnSync('cp', ['-a', path.join(repositoryRoot, 'frontend/node_modules'), path.join(source, 'frontend/node_modules')], { shell: false, stdio: 'inherit' });
  const buildEnv = { ...process.env, NEXT_TELEMETRY_DISABLED: '1' };
  const typecheck = spawnSync('npm', ['run', 'typecheck'], { cwd: path.join(source, 'frontend'), env: buildEnv, shell: false, stdio: 'inherit' });
  if (typecheck.status !== 0) throw new Error('Pinned frontend typecheck failed.');
  const built = spawnSync('npm', ['run', 'build', '--', '--webpack'], { cwd: path.join(source, 'frontend'), env: buildEnv, shell: false, stdio: 'inherit' });
  if (built.status !== 0) throw new Error('Pinned frontend production build failed.');
  const frontendStage = path.join(work, 'frontend-stage');
  const staged = spawnSync(process.execPath, [path.join(source, 'launcher/scripts/package-frontend.mjs'), '--output', frontendStage], { cwd: source, shell: false, stdio: 'inherit' });
  if (staged.status !== 0) throw new Error('Standalone frontend staging failed.');
  const nodeOut = path.join(work, 'node');
  const fetched = spawnSync(process.execPath, [path.join(repositoryRoot, 'launcher/scripts/fetch-node-runtime.mjs'), '--output', nodeOut, '--platform', 'macos-arm64'], { cwd: repositoryRoot, shell: false, stdio: 'inherit' });
  if (fetched.status !== 0) throw new Error('Could not fetch the pinned Darwin ARM64 Node runtime.');
  fs.cpSync(nodeOut, path.join(appRoot, 'runtime'), { recursive: true });
  fs.cpSync(path.join(frontendStage, 'frontend'), path.join(appRoot, 'frontend'), { recursive: true });
  for (const relative of ['bin/speechcraft', 'launcher/src', 'setup-controller/src', 'setup-controller/resources', 'backend/app', 'workers/dataset/speechcraft_dataset', 'workers/dataset/scripts/preflight.py', 'workers/dataset/scripts/runtime_probe.py', 'workers/dataset/runtime-profiles/macos-arm64', 'workers/dataset/runtime-fixtures']) {
    const from = path.join(source, relative);
    const to = path.join(appRoot, relative);
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.cpSync(from, to, { recursive: true });
  }
  const inventory = validateRuntimeInventory(appRoot);
  if (!inventory.ok) throw new Error(`Mac runtime inventory is incomplete: ${inventory.missing.join(', ')}`);
  const release = { schema_version: 1, application: 'SpeechCraft', version, platform: 'macos-arm64', source_revision: sourceRevisionArg,
    node: { version: JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'launcher/resources/node-runtime.json'), 'utf8')).version, executable: 'runtime/node' },
    signing: { status: 'unsigned_development_build', notarized: false },
    notices: ['No macOS code signing or notarization was performed.', 'FFmpeg is built from pinned source on the Mac and is not a redistributed binary.', 'TitaNet provenance remains unresolved.'] };
  fs.writeFileSync(path.join(appRoot, 'application-release-manifest.json'), `${JSON.stringify(release, null, 2)}\n`);
  const checked = validateMacRelease(appRoot);
  if (!checked.ok) throw new Error(`Mac package validation failed: ${(checked.problems || []).concat(checked.missing).join(', ')}`);
  fs.mkdirSync(outputRoot, { recursive: true });
  const bundle = path.join(outputRoot, `${appName}.tar.gz`);
  const packed = spawnSync('tar', ['-czf', bundle, '-C', work, appName], { shell: false, stdio: 'inherit' });
  if (packed.status !== 0) throw new Error('Could not write the macOS archive.');
  const hash = crypto.createHash('sha256').update(fs.readFileSync(bundle)).digest('hex');
  fs.writeFileSync(path.join(outputRoot, `${appName}.tar.gz.sha256`), `${hash}  ${appName}.tar.gz\n`);
  fs.writeFileSync(path.join(outputRoot, `${appName}.tar.gz.release.json`), `${JSON.stringify({ schema_version: 1, platform: 'macos-arm64', archive: `${appName}.tar.gz`, archive_sha256: hash, archive_size_bytes: fs.statSync(bundle).size, source_revision: sourceRevisionArg, application_version: version, signing: release.signing }, null, 2)}\n`);
  console.log(`Built ${bundle}`);
  console.log(`SHA-256 ${hash}`);
  console.log(`Host ${os.platform()}/${os.arch()} bundled Darwin ARM64 Node without notarization.`);
}
