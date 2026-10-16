#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const version = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'frontend/package.json'), 'utf8')).version;
const outputArg = process.argv.indexOf('--output');
const outputRoot = path.resolve(outputArg < 0 ? path.join(repositoryRoot, 'dist/speechcraft-linux') : process.argv[outputArg + 1]);
if (process.platform !== 'linux' || process.arch !== 'x64') throw new Error('Linux release packaging requires Linux x86-64.');
if (outputRoot === repositoryRoot || outputRoot.startsWith(`${repositoryRoot}${path.sep}`) === false) {
  // Keep generated release outputs outside tracked source by default; custom destinations may be external.
  if (outputArg >= 0 && outputRoot.startsWith(`${repositoryRoot}${path.sep}`)) throw new Error('Choose a release output outside the source checkout.');
}
if (fs.existsSync(outputRoot)) throw new Error(`Output already exists: ${outputRoot}`);
const parent = path.dirname(outputRoot);
fs.mkdirSync(parent, { recursive: true, mode: 0o700 });
const work = fs.mkdtempSync(path.join(parent, '.speechcraft-package-'));
const appName = `speechcraft-${version}-linux-x86_64`;
const appRoot = path.join(work, appName);
const run = (command, args, cwd = repositoryRoot) => {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', shell: false, env: process.env });
  if (result.error || result.status !== 0) throw new Error(`${command} ${args.join(' ')} failed${result.error ? `: ${result.error.message}` : ` (exit ${result.status})`}.`);
};
const hashFile = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const bootstrapManifest = JSON.parse(fs.readFileSync(path.join(repositoryRoot, 'setup-controller/resources/bootstrap-manifest.json'), 'utf8'));
async function acquirePinnedUv(destination) {
  const artifact = bootstrapManifest.uv.artifacts['linux-x86_64'];
  const response = await fetch(artifact.url, { redirect: 'follow', signal: AbortSignal.timeout(120_000) });
  if (!response.ok || new URL(response.url).hostname !== 'github.com' && !new URL(response.url).hostname.endsWith('.githubusercontent.com')) {
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
  if (versionResult.status !== 0 || !versionResult.stdout.startsWith(`uv ${bootstrapManifest.uv.version} `)) {
    throw new Error('Verified uv executable reported an unexpected version.');
  }
  fs.unlinkSync(archive);
  return { version: bootstrapManifest.uv.version, archive_sha256: artifact.sha256, executable_sha256: artifact.executable_sha256 };
}
const copyTracked = (relative) => {
  const source = fs.realpathSync(path.join(repositoryRoot, relative));
  if (!source.startsWith(`${repositoryRoot}${path.sep}`)) throw new Error(`Tracked package input escapes the checkout: ${relative}`);
  const target = path.join(appRoot, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.cpSync(source, target, { recursive: true, dereference: true, preserveTimestamps: true, errorOnExist: true });
};
const tracked = spawnSync('git', ['ls-files', '-z'], { cwd: repositoryRoot, encoding: 'utf8', shell: false });
if (tracked.status !== 0) throw new Error('Could not enumerate trusted tracked package inputs.');
const allow = (name) => name === 'launcher/package.json' || name.startsWith('launcher/src/') || name.startsWith('launcher/resources/')
  || name.startsWith('setup-controller/src/') || name.startsWith('setup-controller/resources/') || name === 'setup-controller/package.json'
  || name.startsWith('backend/app/') || ['backend/pyproject.toml', 'backend/uv.lock'].includes(name)
  || name.startsWith('workers/dataset/speechcraft_dataset/') || name === 'workers/dataset/scripts/runtime_probe.py'
  || name.startsWith('workers/dataset/runtime-fixtures/') || name.startsWith('workers/dataset/runtime-profiles/')
  || ['workers/dataset/pyproject.toml', 'workers/dataset/uv.lock', 'workers/dataset/model-catalog.json', 'workers/dataset/model-manifest.schema.json'].includes(name)
  || ['LICENSE', 'launcher/README.md', 'setup-controller/FFMPEG-LICENSE-NOTICE.md', 'workers/dataset/TITANET-ONNX-PROVENANCE.md', 'workers/dataset/RUNTIME_PROFILES.md', 'workers/dataset/MODEL_STORAGE.md', 'packages/model_manager/PROVENANCE.md'].includes(name);

try {
  fs.mkdirSync(appRoot, { recursive: true, mode: 0o755 });
  run('npm', ['run', 'typecheck'], path.join(repositoryRoot, 'frontend'));
  run('npm', ['run', 'build'], path.join(repositoryRoot, 'frontend'));
  const frontendStage = path.join(work, 'frontend-stage');
  run(process.execPath, [path.join(repositoryRoot, 'launcher/scripts/package-frontend.mjs'), '--output', frontendStage]);
  for (const entry of tracked.stdout.split('\0').filter(allow)) copyTracked(entry);
  fs.mkdirSync(path.join(appRoot, 'launcher/src'), { recursive: true });
  fs.copyFileSync(path.join(repositoryRoot, 'launcher/src/cli.js'), path.join(appRoot, 'launcher/src/cli.js'));
  fs.cpSync(path.join(frontendStage, 'frontend'), path.join(appRoot, 'frontend'), { recursive: true, errorOnExist: true });

  fs.mkdirSync(path.join(appRoot, 'bin'), { recursive: true });
  fs.copyFileSync(path.join(repositoryRoot, 'bin/speechcraft'), path.join(appRoot, 'bin/speechcraft'));
  fs.chmodSync(path.join(appRoot, 'bin/speechcraft'), 0o755);
  fs.mkdirSync(path.join(appRoot, 'resources/wheels'), { recursive: true });
  const wheelOut = path.join(work, 'wheel'); fs.mkdirSync(wheelOut);
  const pinnedUv = await acquirePinnedUv(path.join(work, 'uv'));
  run(path.join(work, 'uv'), ['build', '--project', path.join(repositoryRoot, 'packages/model_manager'), '--wheel', '--out-dir', wheelOut]);
  const wheelName = fs.readdirSync(wheelOut).find((name) => name === 'speechcraft_model_manager-0.1.0-py3-none-any.whl');
  if (!wheelName) throw new Error('The trusted model-manager wheel was not produced.');
  fs.copyFileSync(path.join(wheelOut, wheelName), path.join(appRoot, 'resources/wheels', wheelName));
  const wheelSha = hashFile(path.join(appRoot, 'resources/wheels', wheelName));
  const nodeRuntime = path.join(work, 'runtime');
  run(process.execPath, [path.join(repositoryRoot, 'launcher/scripts/fetch-node-runtime.mjs'), '--output', nodeRuntime, '--platform', 'linux-x86_64']);
  fs.cpSync(nodeRuntime, path.join(appRoot, 'runtime'), { recursive: true, errorOnExist: true });
  const runtimeVersion = JSON.parse(fs.readFileSync(path.join(appRoot, 'runtime/runtime-manifest.json'), 'utf8')).version;

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
  const release = { schema_version: 1, application: 'SpeechCraft', version, platform: 'linux-x86_64',
    node: { version: runtimeVersion, executable: 'runtime/node', sha256: hashFile(path.join(appRoot, 'runtime/node')) },
    frontend: { next_build_id: frontendIdentity.next_build_id, files: fileRecords.filter((item) => item.path.startsWith('frontend/')).length },
    setup_controller: { entry: 'setup-controller/src/server.js' },
    runtime_manifest_sha256: runtimeManifestIdentity,
    build_tools: { uv: pinnedUv },
    trusted_inputs: { model_manager_wheel: { path: `resources/wheels/${wheelName}`, sha256: wheelSha } },
    package_files: fileRecords,
    validation: { strict_typecheck: 'passed', next_production_build: 'passed', frontend_staging: 'passed', target_hardware_runtime: 'not_run' },
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
    application_version: version, platform: 'linux-x86_64', node_version: runtimeVersion,
    application_manifest_sha256: crypto.createHash('sha256').update(internalManifest).digest('hex'),
    frontend_build_id: frontendIdentity.next_build_id, runtime_manifest_sha256: runtimeManifestIdentity,
    model_manager_wheel_sha256: wheelSha }, null, 2)}\n`, { flag: 'wx', mode: 0o644 });
  fs.cpSync(path.join(repositoryRoot, 'launcher/installer/install-linux.py'), path.join(outputRoot, 'install-linux.py'));
  fs.cpSync(path.join(repositoryRoot, 'launcher/installer/install-linux.sh'), path.join(outputRoot, 'install-linux.sh'));
  fs.chmodSync(path.join(outputRoot, 'install-linux.sh'), 0o755);
  console.log(`Built ${path.join(outputRoot, path.basename(archive))}`);
  console.log(`SHA-256 ${archiveSha}; ${fileRecords.length} application payload files; Node.js ${runtimeVersion}; model-manager wheel ${wheelSha}.`);
} catch (error) {
  fs.rmSync(outputRoot, { recursive: true, force: true });
  throw error;
} finally { fs.rmSync(work, { recursive: true, force: true }); }
