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

function copyPinnedSource(sourceRoot, appRoot, relative) {
  const rootReal = fs.realpathSync(sourceRoot);
  const source = path.join(sourceRoot, relative);
  const target = path.join(appRoot, relative);
  const copyEntry = (from, to) => {
    const info = fs.lstatSync(from);
    if (info.isSymbolicLink()) {
      const resolved = fs.realpathSync(from);
      if (!resolved.startsWith(`${rootReal}${path.sep}`)) throw new Error(`Runtime source symlink escapes the pinned snapshot: ${path.relative(sourceRoot, from)}`);
      return copyEntry(resolved, to);
    }
    if (info.isDirectory()) {
      fs.mkdirSync(to, { recursive: true, mode: info.mode & 0o777 });
      for (const name of fs.readdirSync(from).sort()) copyEntry(path.join(from, name), path.join(to, name));
    } else if (info.isFile()) {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
      fs.chmodSync(to, info.mode & 0o777);
    } else throw new Error(`Unsupported source entry in pinned snapshot: ${path.relative(sourceRoot, from)}`);
  };
  copyEntry(source, target);
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
  const machO = (header[0] === 0xcf && header[1] === 0xfa && header[2] === 0xed && header[3] === 0xfe)
    || (header[0] === 0xfe && header[1] === 0xed && header[2] === 0xfa && header[3] === 0xcf);
  if (!machO) problems.push('bundled node is not a 64-bit Mach-O executable');
  const records = release.package_files;
  if (!Array.isArray(records) || records.length === 0) problems.push('missing package file inventory');
  else {
    const actual = new Set();
    const root = path.resolve(directory);
    const visit = (folder) => {
      for (const item of fs.readdirSync(folder, { withFileTypes: true })) {
        const filename = path.join(folder, item.name);
        const stat = fs.lstatSync(filename);
        if (stat.isSymbolicLink()) throw new Error(`Mac package contains a symlink: ${path.relative(root, filename)}`);
        if (item.isDirectory()) visit(filename);
        else if (item.isFile() && path.resolve(filename) !== path.resolve(releasePath)) actual.add(path.relative(root, filename).split(path.sep).join('/'));
        else if (!item.isFile()) throw new Error(`Mac package contains an unsupported entry: ${path.relative(root, filename)}`);
      }
    };
    visit(root);
    const seen = new Set();
    for (const record of records) {
      if (!record || typeof record.path !== 'string' || path.isAbsolute(record.path) || record.path.split('/').includes('..')
          || seen.has(record.path) || !/^[a-f0-9]{64}$/.test(record.sha256 || '') || !Number.isSafeInteger(record.size_bytes)) problems.push('invalid package file inventory record');
      else {
        seen.add(record.path);
        const filename = path.join(root, record.path);
        try {
          const stat = fs.lstatSync(filename);
          if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== record.size_bytes
              || crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex') !== record.sha256
              || Boolean(stat.mode & 0o111) !== (record.executable === true)) problems.push(`package inventory mismatch: ${record.path}`);
        } catch { problems.push(`package inventory missing: ${record.path}`); }
      }
    }
    if (actual.size !== seen.size || [...actual].some((name) => !seen.has(name))) problems.push('package file inventory is incomplete or contains extra files');
    if (!release.trusted_inputs?.model_manager_wheel?.path || !/^[a-f0-9]{64}$/.test(release.trusted_inputs?.model_manager_wheel?.sha256 || '')) problems.push('model-manager wheel identity');
    if (!release.node?.sha256 || !/^[a-f0-9]{64}$/.test(release.node.sha256)) problems.push('Node runtime digest');
    if (!release.runtime_manifest_sha256 || !/^[a-f0-9]{64}$/.test(release.runtime_manifest_sha256)) problems.push('runtime manifest digest');
    if (!release.frontend?.next_build_id || !release.frontend?.files) problems.push('frontend identity');
  }
  return { ok: problems.length === 0, missing, problems, release };
}

async function acquirePinnedUv(destination) {
  const bootstrap = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'setup-controller/resources/bootstrap-manifest.json'), 'utf8'));
  const artifact = bootstrap.uv.artifacts['macos-arm64'];
  if (!artifact) throw new Error('Pinned build-tools manifest has no macOS ARM64 uv artifact.');
  const response = await fetch(artifact.url, { redirect: 'follow', signal: AbortSignal.timeout(120_000) });
  if (!response.ok || !response.body || !new URL(response.url).hostname.endsWith('githubusercontent.com') && new URL(response.url).hostname !== 'github.com') throw new Error('Could not acquire pinned uv from its trusted GitHub release.');
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length !== artifact.size_bytes || crypto.createHash('sha256').update(bytes).digest('hex') !== artifact.sha256) throw new Error('Pinned macOS uv archive failed its size or SHA-256 check.');
  const archive = `${destination}.tar.gz`;
  fs.writeFileSync(archive, bytes, { flag: 'wx', mode: 0o600 });
  const extracted = spawnSync('tar', ['-xOf', archive, `${artifact.tar_root}/${artifact.executable}`], { shell: false, maxBuffer: 80 * 1024 * 1024 });
  if (extracted.error || extracted.status !== 0 || extracted.stdout.length !== artifact.executable_size_bytes || crypto.createHash('sha256').update(extracted.stdout).digest('hex') !== artifact.executable_sha256) throw new Error('Pinned macOS uv executable did not match the trusted bootstrap manifest.');
  fs.writeFileSync(destination, extracted.stdout, { flag: 'wx', mode: 0o700 });
  const version = spawnSync(destination, ['--version'], { encoding: 'utf8', shell: false });
  if (version.status !== 0 || !version.stdout.startsWith(`uv ${bootstrap.uv.version} `)) throw new Error('Verified uv executable reported an unexpected version.');
  fs.unlinkSync(archive);
  return { version: bootstrap.uv.version, archive_sha256: artifact.sha256, executable_sha256: artifact.executable_sha256 };
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
  assertDarwinArm64();
  const revision = spawnSync('git', ['rev-parse', `${sourceRevisionArg}^{commit}`], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
  if (revision.status !== 0 || revision.stdout.trim() !== sourceRevisionArg) throw new Error('The requested source revision did not resolve exactly.');
  const checkout = spawnSync('git', ['rev-parse', 'HEAD'], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
  const changes = spawnSync('git', ['status', '--porcelain'], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
  if (checkout.status !== 0 || checkout.stdout.trim() !== sourceRevisionArg || changes.status !== 0 || changes.stdout.trim()) {
    throw new Error('Mac packaging requires a clean checkout at the exact source revision so build scripts and application sources share one identity.');
  }
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
  const exported = spawnSync('git', ['archive', '--format=tar', sourceRevisionArg, '--', 'bin', 'frontend', 'launcher', 'setup-controller/src', 'setup-controller/resources', 'setup-controller/package.json', 'backend/app', 'backend/pyproject.toml', 'backend/uv.lock', 'workers/dataset', 'packages/model_manager', 'LICENSE'], { cwd: repositoryRoot, shell: false, stdio: ['ignore', archiveFd, 'inherit'] });
  fs.closeSync(archiveFd);
  if (exported.status !== 0) throw new Error(`Could not export pinned source revision ${sourceRevisionArg}.`);
  const source = path.join(work, 'source');
  fs.mkdirSync(source);
  spawnSync('tar', ['-xf', archive, '-C', source], { shell: false, stdio: 'inherit' });
  const dependencyInstall = spawnSync('bun', ['install', '--frozen-lockfile'], { cwd: path.join(source, 'frontend'), shell: false, stdio: 'inherit' });
  if (dependencyInstall.error || dependencyInstall.status !== 0) throw new Error('Could not install the exact frontend dependency lock in the pinned source snapshot.');
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
  fs.copyFileSync(path.join(frontendStage, 'frontend-release-manifest.json'), path.join(appRoot, 'frontend-release-manifest.json'));
  for (const relative of [
    'bin/speechcraft', 'launcher/package.json', 'launcher/src', 'setup-controller/package.json', 'setup-controller/src', 'setup-controller/resources',
    'backend/app', 'backend/pyproject.toml', 'backend/uv.lock',
    'workers/dataset/speechcraft_dataset', 'workers/dataset/scripts/preflight.py', 'workers/dataset/scripts/runtime_probe.py',
    'workers/dataset/pyproject.toml', 'workers/dataset/uv.lock', 'workers/dataset/model-catalog.json', 'workers/dataset/model-manifest.schema.json',
    'workers/dataset/runtime-profiles/macos-arm64', 'workers/dataset/runtime-profiles/linux-cpu', 'workers/dataset/runtime-fixtures',
    'packages/model_manager/pyproject.toml', 'packages/model_manager/README.md', 'packages/model_manager/PROVENANCE.md', 'packages/model_manager/speechcraft_models',
    'LICENSE', 'launcher/README.md', 'workers/dataset/RUNTIME_PROFILES.md', 'workers/dataset/MODEL_STORAGE.md',
  ]) {
    const from = path.join(source, relative);
    if (!fs.existsSync(from)) throw new Error(`Pinned source snapshot is missing required resource: ${relative}`);
    copyPinnedSource(source, appRoot, relative);
  }
  const wheelOut = path.join(work, 'wheel');
  fs.mkdirSync(wheelOut);
  const uv = path.join(work, 'uv');
  const uvIdentity = await acquirePinnedUv(uv);
  const wheel = spawnSync(uv, ['build', '--project', path.join(source, 'packages/model_manager'), '--wheel', '--out-dir', wheelOut], { shell: false, stdio: 'inherit' });
  if (wheel.status !== 0) throw new Error('The trusted model-manager wheel was not produced. Install the pinned uv tool and retry.');
  const wheelName = fs.readdirSync(wheelOut).find((name) => name.endsWith('.whl'));
  if (!wheelName) throw new Error('The trusted model-manager wheel was not produced.');
  fs.mkdirSync(path.join(appRoot, 'resources/wheels'), { recursive: true });
  fs.copyFileSync(path.join(wheelOut, wheelName), path.join(appRoot, 'resources/wheels', wheelName));
  const inventory = validateRuntimeInventory(appRoot);
  if (!inventory.ok) throw new Error(`Mac runtime inventory is incomplete: ${inventory.missing.join(', ')}`);
  const wheelSha = crypto.createHash('sha256').update(fs.readFileSync(path.join(appRoot, 'resources/wheels', wheelName))).digest('hex');
  const nodeManifest = JSON.parse(fs.readFileSync(path.join(appRoot, 'runtime/runtime-manifest.json'), 'utf8'));
  const runtimeManifestSha = crypto.createHash('sha256').update(fs.readFileSync(path.join(appRoot, 'setup-controller/resources/runtime-manifest.json'))).digest('hex');
  const frontendManifest = JSON.parse(fs.readFileSync(path.join(frontendStage, 'frontend-release-manifest.json'), 'utf8'));
  const packageFiles = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const filename = path.join(directory, entry.name);
      const stat = fs.lstatSync(filename);
      const relative = path.relative(appRoot, filename).split(path.sep).join('/');
      if (stat.isSymbolicLink()) throw new Error(`Mac package contains a symlink: ${relative}`);
      if (entry.isDirectory()) visit(filename);
      else if (stat.isFile() && relative !== 'application-release-manifest.json') packageFiles.push({ path: relative, size_bytes: stat.size,
        sha256: crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex'), executable: Boolean(stat.mode & 0o111) });
      else throw new Error(`Unsupported Mac package entry: ${relative}`);
    }
  };
  visit(appRoot);
  const release = { schema_version: 1, application: 'SpeechCraft', version, platform: 'macos-arm64', source_revision: sourceRevisionArg,
    build_platform: `${os.platform()}-${os.arch()}`,
    node: { version: nodeManifest.version, executable: 'runtime/node', sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(appRoot, 'runtime/node'))).digest('hex'), artifact: nodeManifest.artifact, artifact_sha256: nodeManifest.artifact_sha256 },
    frontend: { next_build_id: frontendManifest.next_build_id, files: frontendManifest.files.length, dependency_lock_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(source, 'frontend/bun.lock'))).digest('hex') },
    runtime_manifest_sha256: runtimeManifestSha, build_tools: { uv: uvIdentity },
    trusted_inputs: { model_manager_wheel: { path: `resources/wheels/${wheelName}`, sha256: wheelSha } },
    runtime_inventory: { status: 'complete', required_files: MAC_REQUIRED_FILES }, package_files: packageFiles,
    validation: { target_build: 'native-macos-arm64', frontend_staging: 'passed', runtime_inventory: 'passed', macos_execution: 'not_run' },
    signing: { status: 'unsigned_development_build', notarized: false },
    notices: ['No macOS code signing or notarization was performed.', 'FFmpeg is built from pinned source on the Mac and is not a redistributed binary.', 'WeSpeaker ECAPA model provenance and CC BY 4.0 attribution are documented in the release.'] };
  fs.writeFileSync(path.join(appRoot, 'application-release-manifest.json'), `${JSON.stringify(release, null, 2)}\n`);
  const checked = validateMacRelease(appRoot);
  if (!checked.ok) throw new Error(`Mac package validation failed: ${(checked.problems || []).concat(checked.missing).join(', ')}`);
  const executableCandidates = checked.release.package_files.filter((file) => file.path.endsWith('.node') || file.path === 'runtime/node');
  for (const file of executableCandidates) {
    const architecture = spawnSync('lipo', ['-archs', path.join(appRoot, file.path)], { encoding: 'utf8', shell: false });
    if (architecture.status !== 0 || architecture.stdout.trim() !== 'arm64') throw new Error(`Executable ${file.path} is not a verified Darwin ARM64 binary.`);
  }
  fs.mkdirSync(outputRoot, { recursive: true });
  const bundle = path.join(outputRoot, `${appName}.tar.gz`);
  const packed = spawnSync('python3', [path.join(repositoryRoot, 'launcher/scripts/package-archive.py'), work, appName, bundle], { shell: false, stdio: 'inherit' });
  if (packed.status !== 0) throw new Error('Could not write the macOS archive.');
  const hash = crypto.createHash('sha256').update(fs.readFileSync(bundle)).digest('hex');
  fs.writeFileSync(path.join(outputRoot, `${appName}.tar.gz.sha256`), `${hash}  ${appName}.tar.gz\n`);
  fs.writeFileSync(path.join(outputRoot, `${appName}.tar.gz.release.json`), `${JSON.stringify({ schema_version: 1, platform: 'macos-arm64', archive: `${appName}.tar.gz`, archive_sha256: hash, archive_size_bytes: fs.statSync(bundle).size, source_revision: sourceRevisionArg, application_version: version, application_manifest_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(appRoot, 'application-release-manifest.json'))).digest('hex'), node_version: nodeManifest.version, runtime_manifest_sha256: runtimeManifestSha, model_manager_wheel_sha256: wheelSha, signing: release.signing }, null, 2)}\n`);
  fs.copyFileSync(path.join(source, 'launcher/installer/install-macos.sh'), path.join(outputRoot, 'install-macos.sh'));
  console.log(`Built ${bundle}`);
  console.log(`SHA-256 ${hash}`);
  console.log(`Host ${os.platform()}/${os.arch()} bundled Darwin ARM64 Node without notarization.`);
}
