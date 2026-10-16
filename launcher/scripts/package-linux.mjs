#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { validateRuntimeInventory } from '../src/package-inventory.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const argv = process.argv.slice(2);
const valueAfter = (flag) => { const index = argv.indexOf(flag); return index < 0 ? null : argv[index + 1] || null; };
const outputArg = valueAfter('--output');
if (!outputArg) throw new Error('Usage: node launcher/scripts/package-linux.mjs --output ABSOLUTE_OUTPUT_DIRECTORY');
const outputRoot = path.resolve(outputArg);
const fromSnapshot = argv.includes('--snapshot-build');
const sourceRevisionArg = valueAfter('--source-revision');
if (process.platform !== 'linux' || process.arch !== 'x64') throw new Error('Linux release packaging requires Linux x86-64.');
if (!path.isAbsolute(outputArg) || outputRoot.startsWith(`${repositoryRoot}${path.sep}`)) throw new Error('Choose an absolute release output directory outside the source checkout.');
if (fs.existsSync(outputRoot)) throw new Error(`Output already exists: ${outputRoot}`);
fs.mkdirSync(path.dirname(outputRoot), { recursive: true, mode: 0o700 });
const work = fs.mkdtempSync(path.join(path.dirname(outputRoot), '.speechcraft-package-'));
const run = (command, args, cwd = repositoryRoot, env = process.env, maxBuffer = 16 * 1024 * 1024) => {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', shell: false, env, maxBuffer });
  if (result.error || result.status !== 0) throw new Error(`${command} ${args.join(' ')} failed${result.error ? `: ${result.error.message}` : ` (exit ${result.status})`}.`);
};
const runCapture = (command, args, cwd = repositoryRoot) => {
  const result = spawnSync(command, args, { cwd, encoding: 'utf8', shell: false, maxBuffer: 16 * 1024 * 1024 });
  if (result.error || result.status !== 0) throw new Error(`${command} ${args.join(' ')} failed: ${result.stderr || result.error?.message || result.status}`);
  return result.stdout.trim();
};
const hashFile = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

async function acquirePinnedUv(destination, bootstrapManifest) {
  const artifact = bootstrapManifest.uv.artifacts['linux-x86_64'];
  const response = await fetch(artifact.url, { redirect: 'follow', signal: AbortSignal.timeout(120_000) });
  if (!response.ok || (new URL(response.url).hostname !== 'github.com' && !new URL(response.url).hostname.endsWith('.githubusercontent.com'))) {
    throw new Error('Could not acquire the pinned Astral uv release from its trusted GitHub release.');
  }
  const archiveBytes = Buffer.from(await response.arrayBuffer());
  if (archiveBytes.length !== artifact.size_bytes || crypto.createHash('sha256').update(archiveBytes).digest('hex') !== artifact.sha256) {
    throw new Error('Pinned uv release archive failed its trusted size or SHA-256 check.');
  }
  const archive = `${destination}.tar.gz`;
  fs.writeFileSync(archive, archiveBytes, { flag: 'wx', mode: 0o600 });
  const extracted = spawnSync('tar', ['-xOf', archive, `${artifact.tar_root}/${artifact.executable}`], { maxBuffer: 80 * 1024 * 1024, shell: false });
  if (extracted.error || extracted.status !== 0 || extracted.stdout.length !== artifact.executable_size_bytes
      || crypto.createHash('sha256').update(extracted.stdout).digest('hex') !== artifact.executable_sha256) {
    throw new Error('Pinned uv executable did not match the trusted bootstrap manifest.');
  }
  fs.writeFileSync(destination, extracted.stdout, { flag: 'wx', mode: 0o700 });
  const versionResult = spawnSync(destination, ['--version'], { encoding: 'utf8', shell: false });
  if (versionResult.status !== 0 || !versionResult.stdout.startsWith(`uv ${bootstrapManifest.uv.version} `)) throw new Error('Verified uv executable reported an unexpected version.');
  fs.unlinkSync(archive);
  return { version: bootstrapManifest.uv.version, archive_sha256: artifact.sha256, executable_sha256: artifact.executable_sha256 };
}

function copySafe(sourceRoot, appRoot, relative) {
  const source = path.join(sourceRoot, relative);
  const target = path.join(appRoot, relative);
  const rootReal = fs.realpathSync(sourceRoot);
  const copyEntry = (from, to) => {
    const stat = fs.lstatSync(from);
    if (stat.isSymbolicLink()) {
      const resolved = fs.realpathSync(from);
      if (!resolved.startsWith(`${rootReal}${path.sep}`)) throw new Error(`Runtime source symlink escapes its pinned snapshot: ${path.relative(sourceRoot, from)}`);
      return copyEntry(resolved, to);
    }
    if (stat.isDirectory()) {
      fs.mkdirSync(to, { recursive: true, mode: stat.mode & 0o777 });
      for (const name of fs.readdirSync(from).sort()) copyEntry(path.join(from, name), path.join(to, name));
    } else if (stat.isFile()) {
      fs.mkdirSync(path.dirname(to), { recursive: true });
      fs.copyFileSync(from, to, fs.constants.COPYFILE_EXCL);
      fs.chmodSync(to, stat.mode & 0o777);
    } else throw new Error(`Unsupported runtime source object: ${path.relative(sourceRoot, from)}`);
  };
  if (!fs.existsSync(source)) throw new Error(`Pinned source snapshot is missing required resource: ${relative}`);
  copyEntry(source, target);
}

async function outerSnapshotBuild() {
  if (!/^[a-f0-9]{40}$/.test(sourceRevisionArg || '')) throw new Error('The source revision must be an exact 40-character Git commit hash.');
  const revision = runCapture('git', ['rev-parse', `${sourceRevisionArg}^{commit}`]);
  if (revision !== sourceRevisionArg) throw new Error('The requested source revision did not resolve exactly.');
  const packageInputs = runCapture('git', ['status', '--porcelain', '--', 'frontend/package.json', 'frontend/bun.lock']);
  if (packageInputs) throw new Error('Frontend dependency manifests are modified locally; commit them before a provenance-bound release build.');
  const archive = path.join(work, 'source.tar');
  const archiveFd = fs.openSync(archive, 'w');
  const archived = spawnSync('git', ['archive', '--format=tar', revision, '--', 'bin', 'frontend', 'launcher', 'setup-controller/src', 'setup-controller/resources', 'setup-controller/package.json', 'backend/app', 'backend/pyproject.toml', 'backend/uv.lock', 'workers/dataset', 'packages/model_manager', 'LICENSE'], { cwd: repositoryRoot, shell: false, stdio: ['ignore', archiveFd, 'inherit'] });
  fs.closeSync(archiveFd);
  if (archived.error || archived.status !== 0) throw new Error(`Could not export pinned source revision ${revision}.`);
  const snapshotRoot = path.join(work, 'source'); fs.mkdirSync(snapshotRoot, { mode: 0o700 });
  run('tar', ['-xf', archive, '-C', snapshotRoot]);
  fs.unlinkSync(archive);
  const installedDependencies = path.join(repositoryRoot, 'frontend/node_modules');
  if (!fs.existsSync(path.join(installedDependencies, 'next/package.json')) || !fs.existsSync(path.join(installedDependencies, 'typescript/package.json')))
    throw new Error('Frontend build dependencies are missing. Install dependencies according to frontend/bun.lock before packaging.');
  const expectedNext = JSON.parse(fs.readFileSync(path.join(snapshotRoot, 'frontend/package.json'), 'utf8')).dependencies?.next;
  const actualNext = JSON.parse(fs.readFileSync(path.join(installedDependencies, 'next/package.json'), 'utf8')).version;
  if (expectedNext !== actualNext) throw new Error(`Pinned build input mismatch: source requires Next.js ${expectedNext}, installed dependency tree has ${actualNext}.`);
  // Turbopack refuses node_modules symlinks that escape its project root.
  // Copy only the prepared build dependency tree into the exported source
  // snapshot; all frontend/backend source still comes from the pinned commit.
  run('cp', ['-a', installedDependencies, path.join(snapshotRoot, 'frontend/node_modules')], repositoryRoot);
  run(process.execPath, [path.join(snapshotRoot, 'launcher/scripts/package-linux.mjs'), '--snapshot-build', '--source-revision', revision, '--output', outputRoot], snapshotRoot, process.env);
}

async function snapshotBuild() {
  const sourceRevision = sourceRevisionArg;
  if (!/^[a-f0-9]{40}$/.test(sourceRevision || '')) throw new Error('Snapshot build is missing its pinned source revision.');
  const version = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'frontend/package.json'), 'utf8')).version;
  const appName = `speechcraft-${version}-linux-x86_64`;
  const appRoot = path.join(work, appName);
  try {
    fs.mkdirSync(appRoot, { recursive: true, mode: 0o755 });
    const nodeRuntime = path.join(work, 'runtime');
    run(process.execPath, [path.join(repositoryRoot, 'launcher/scripts/fetch-node-runtime.mjs'), '--output', nodeRuntime, '--platform', 'linux-x86_64']);
    const runtimeVersion = JSON.parse(fs.readFileSync(path.join(nodeRuntime, 'runtime-manifest.json'), 'utf8')).version;
    const buildEnv = { ...process.env, NEXT_TELEMETRY_DISABLED: '1', PATH: `${nodeRuntime}${path.delimiter}${process.env.PATH || ''}` };
    run('npm', ['run', 'typecheck'], path.join(repositoryRoot, 'frontend'), buildEnv);
    // Next 16 Turbopack currently panics during the generated global-error
    // route's prerender in this app. Webpack is the supported production
    // builder and keeps the release build deterministic while that upstream
    // regression is unresolved.
    run('npm', ['run', 'build', '--', '--webpack'], path.join(repositoryRoot, 'frontend'), buildEnv);
    const frontendStage = path.join(work, 'frontend-stage');
    run(process.execPath, [path.join(repositoryRoot, 'launcher/scripts/package-frontend.mjs'), '--output', frontendStage], repositoryRoot, buildEnv);

    const copyPaths = [
      'launcher/package.json', 'launcher/src', 'launcher/resources',
      'setup-controller/package.json', 'setup-controller/src', 'setup-controller/resources',
      'backend/app', 'backend/pyproject.toml', 'backend/uv.lock',
      'workers/dataset/speechcraft_dataset', 'workers/dataset/scripts/preflight.py', 'workers/dataset/scripts/runtime_probe.py',
      'workers/dataset/runtime-fixtures', 'workers/dataset/runtime-profiles', 'workers/dataset/pyproject.toml',
      'workers/dataset/uv.lock', 'workers/dataset/model-catalog.json', 'workers/dataset/model-manifest.schema.json',
      'LICENSE', 'launcher/README.md',
      'workers/dataset/TITANET-ONNX-PROVENANCE.md', 'workers/dataset/RUNTIME_PROFILES.md',
      'workers/dataset/MODEL_STORAGE.md', 'packages/model_manager/PROVENANCE.md',
    ];
    for (const relative of copyPaths) copySafe(repositoryRoot, appRoot, relative);
    fs.cpSync(path.join(frontendStage, 'frontend'), path.join(appRoot, 'frontend'), { recursive: true, errorOnExist: true });
    fs.mkdirSync(path.join(appRoot, 'bin'), { recursive: true });
    copySafe(repositoryRoot, appRoot, 'bin/speechcraft');
    fs.chmodSync(path.join(appRoot, 'bin/speechcraft'), 0o755);

    const inventory = validateRuntimeInventory(appRoot);
    if (!inventory.ok) throw new Error(`Packaged runtime resource inventory is incomplete: ${inventory.missing.join(', ')}`);
    const bootstrapManifest = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'setup-controller/resources/bootstrap-manifest.json'), 'utf8'));
    const wheelOut = path.join(work, 'wheel'); fs.mkdirSync(wheelOut);
    const pinnedUv = await acquirePinnedUv(path.join(work, 'uv'), bootstrapManifest);
    run(path.join(work, 'uv'), ['build', '--project', path.join(repositoryRoot, 'packages/model_manager'), '--wheel', '--out-dir', wheelOut]);
    const wheelName = fs.readdirSync(wheelOut).find((name) => name === 'speechcraft_model_manager-0.1.0-py3-none-any.whl');
    if (!wheelName) throw new Error('The trusted model-manager wheel was not produced.');
    fs.mkdirSync(path.join(appRoot, 'resources/wheels'), { recursive: true });
    fs.copyFileSync(path.join(wheelOut, wheelName), path.join(appRoot, 'resources/wheels', wheelName));
    const wheelSha = hashFile(path.join(appRoot, 'resources/wheels', wheelName));

    fs.cpSync(nodeRuntime, path.join(appRoot, 'runtime'), { recursive: true, errorOnExist: true });
    const fileRecords = [];
    const visit = (directory) => {
      for (const item of fs.readdirSync(directory, { withFileTypes: true })) {
        const filename = path.join(directory, item.name);
        const relative = path.relative(appRoot, filename).split(path.sep).join('/');
        const stat = fs.lstatSync(filename);
        if (stat.isSymbolicLink()) throw new Error(`Release contains an unsafe symbolic link: ${relative}`);
        if (item.isDirectory()) visit(filename);
        else if (item.isFile()) fileRecords.push({ path: relative, size_bytes: stat.size, sha256: hashFile(filename), executable: Boolean(stat.mode & 0o111) });
        else throw new Error(`Release contains an unsupported filesystem object: ${relative}`);
      }
    };
    visit(appRoot);
    const runtimeManifestIdentity = hashFile(path.join(appRoot, 'setup-controller/resources/runtime-manifest.json'));
    const frontendIdentity = JSON.parse(fs.readFileSync(path.join(frontendStage, 'frontend-release-manifest.json'), 'utf8'));
    const buildIdentity = { source_revision: sourceRevision, build_platform: `${os.platform()}-${os.arch()}`,
      frontend_dependency_lock_sha256: hashFile(path.join(repositoryRoot, 'frontend/bun.lock')) };
    const release = { schema_version: 1, application: 'SpeechCraft', version, platform: 'linux-x86_64', ...buildIdentity,
      node: { version: runtimeVersion, executable: 'runtime/node', sha256: hashFile(path.join(appRoot, 'runtime/node')) },
      frontend: { next_build_id: frontendIdentity.next_build_id, files: fileRecords.filter((item) => item.path.startsWith('frontend/')).length },
      setup_controller: { entry: 'setup-controller/src/server.js' }, runtime_manifest_sha256: runtimeManifestIdentity,
      build_tools: { uv: pinnedUv }, trusted_inputs: { model_manager_wheel: { path: `resources/wheels/${wheelName}`, sha256: wheelSha } },
      runtime_inventory: { status: 'complete', required_files: REQUIRED_RUNTIME_FILES }, package_files: fileRecords,
      validation: { strict_typecheck: 'passed', next_production_build: 'passed', frontend_staging: 'passed', runtime_inventory: 'passed', target_model_hardware: 'not_run' },
      notices: ['See LICENSE and included component provenance/license notices.', 'Public redistribution remains subject to FFmpeg and TitaNet review.'] };
    fs.writeFileSync(path.join(appRoot, 'application-release-manifest.json'), `${JSON.stringify(release, null, 2)}\n`, { flag: 'wx', mode: 0o644 });

    const archive = path.join(work, `${appName}.tar.gz`);
    run('tar', ['--sort=name', '--mtime=@0', '--owner=0', '--group=0', '--numeric-owner', '-czf', archive, '-C', work, appName]);
    const archiveSha = hashFile(archive);
    fs.mkdirSync(outputRoot, { recursive: true });
    const archiveName = path.basename(archive);
    const publishedArchive = path.join(outputRoot, archiveName);
    fs.renameSync(archive, publishedArchive);
    fs.writeFileSync(path.join(outputRoot, `${archiveName}.sha256`), `${archiveSha}  ${archiveName}\n`, { flag: 'wx', mode: 0o644 });
    const internalManifest = fs.readFileSync(path.join(appRoot, 'application-release-manifest.json'));
    fs.writeFileSync(path.join(outputRoot, `${archiveName}.release.json`), `${JSON.stringify({ schema_version: 1,
      archive: archiveName, archive_size_bytes: fs.statSync(publishedArchive).size, archive_sha256: archiveSha,
      application_version: version, platform: 'linux-x86_64', source_revision: sourceRevision, node_version: runtimeVersion,
      application_manifest_sha256: crypto.createHash('sha256').update(internalManifest).digest('hex'),
      frontend_build_id: frontendIdentity.next_build_id, runtime_manifest_sha256: runtimeManifestIdentity,
      model_manager_wheel_sha256: wheelSha }, null, 2)}\n`, { flag: 'wx', mode: 0o644 });
    fs.copyFileSync(path.join(repositoryRoot, 'launcher/installer/install-linux.py'), path.join(outputRoot, 'install-linux.py'));
    fs.copyFileSync(path.join(repositoryRoot, 'launcher/installer/install-linux.sh'), path.join(outputRoot, 'install-linux.sh'));
    fs.chmodSync(path.join(outputRoot, 'install-linux.sh'), 0o755);
    console.log(`Built ${path.join(outputRoot, archiveName)} from ${sourceRevision}`);
    console.log(`SHA-256 ${archiveSha}; ${fileRecords.length} payload files; Node.js ${runtimeVersion}; worker preflight and runtime helper inventory verified.`);
  } catch (error) {
    fs.rmSync(outputRoot, { recursive: true, force: true });
    throw error;
  }
}

try {
  if (fromSnapshot) await snapshotBuild();
  else await outerSnapshotBuild();
} finally { fs.rmSync(work, { recursive: true, force: true }); }
