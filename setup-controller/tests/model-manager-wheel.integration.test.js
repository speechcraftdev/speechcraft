import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { BootstrapTasks } from '../src/bootstrap.js';

const repository = fileURLToPath(new URL('../../', import.meta.url));
const uvPath = process.env.SPEECHCRAFT_PINNED_UV_PATH;

function sha256(file) { return createHash('sha256').update(fs.readFileSync(file)).digest('hex'); }

function run(executable, args, options = {}) {
  const result = spawnSync(executable, args, { encoding: 'utf8', maxBuffer: 4 * 1024 * 1024, ...options });
  assert.equal(result.error, undefined, result.error?.message);
  assert.equal(result.status, 0, `${path.basename(executable)} ${args.join(' ')} failed (${result.status}): ${result.stderr}`);
  return result.stdout.trim();
}

function context() {
  return { attemptId: '00000000-0000-4000-8000-000000000001', signal: new AbortController().signal,
    onChildProcess: () => true, onProgress() {}, onComponentStatus() {} };
}

test('pinned uv real wheel install verifies without optional archive hashes and reuses after restart', {
  skip: process.platform !== 'linux' || process.arch !== 'x64' || !uvPath,
  timeout: 600_000,
}, async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-manager-wheel-real-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const tasks = new BootstrapTasks({ dataDirectory: root });
  tasks.environment = { profile: 'linux-x86_64', wsl: false };

  const uvArtifact = tasks.manifest.uv.artifacts['linux-x86_64'];
  assert.equal(sha256(uvPath), uvArtifact.executable_sha256, 'the test uv executable must match the pinned manifest');
  assert.equal(run(uvPath, ['--version']).split(' ')[1], tasks.manifest.uv.version);
  tasks.paths = tasks.getPaths('linux-x86_64', { allowMissingManager: true });
  const env = tasks.makeEnvironment(tasks.paths);
  const wheelDirectory = path.join(root, 'wheel');
  fs.mkdirSync(wheelDirectory, { mode: 0o700 });

  run(uvPath, ['build', '--no-config', '--wheel', '--out-dir', wheelDirectory,
    path.join(repository, 'packages/model_manager')], { cwd: root, env });
  const wheels = fs.readdirSync(wheelDirectory).filter((name) => name.endsWith('.whl'));
  assert.equal(wheels.length, 1, 'the package build must produce exactly one wheel');
  tasks.wheelPath = path.join(wheelDirectory, wheels[0]);
  tasks.wheelSha256 = sha256(tasks.wheelPath);
  tasks.paths = tasks.getPaths('linux-x86_64');
  const isolatedEnv = tasks.makeEnvironment(tasks.paths);

  run(uvPath, ['python', 'install', '--no-config', '--managed-python', '--no-bin', '3.12.15'], { cwd: root, env: isolatedEnv });
  fs.mkdirSync(path.dirname(tasks.paths.uvExecutable), { recursive: true, mode: 0o700 });
  fs.copyFileSync(uvPath, tasks.paths.uvExecutable);
  fs.chmodSync(tasks.paths.uvExecutable, 0o755);
  fs.mkdirSync(path.dirname(tasks.paths.environmentRoot), { recursive: true, mode: 0o700 });
  await tasks.ensureModelManager(context());
  const environmentPython = path.join(tasks.paths.environmentRoot, 'bin', 'python');

  const metadata = run(environmentPython, ['-I', '-B', '-c', String.raw`import importlib.metadata as m,json
d=m.distribution('speechcraft-model-manager'); p=d.locate_file(d._path.name + '/direct_url.json')
try:
 x=json.loads(open(p,encoding='utf-8').read()); a=x.get('archive_info',{}); h=a.get('hashes',{}).get('sha256') or a.get('hash','')
 print(json.dumps({'exists':True,'has_archive_hash':bool(h)}))
except FileNotFoundError: print(json.dumps({'exists':False,'has_archive_hash':False}))`]);
  const directUrl = JSON.parse(metadata);
  assert.equal(directUrl.has_archive_hash, false, 'integration must exercise uv metadata without an archive hash');

  await tasks.verifyModelManager(environmentPython, context());
  const siteRoot = run(environmentPython, ['-I', '-B', '-c', "import importlib.metadata as m; print(m.distribution('speechcraft-model-manager').locate_file(''))"]);
  const distInfo = fs.readdirSync(siteRoot).find((name) => name.startsWith('speechcraft_model_manager-') && name.endsWith('.dist-info'));
  assert.ok(distInfo);
  const directPath = path.join(siteRoot, distInfo, 'direct_url.json');
  fs.writeFileSync(directPath, JSON.stringify({ url: 'file:///redacted/local-wheel.whl', archive_info: { hashes: { sha256: tasks.wheelSha256 } } }), { mode: 0o600 });
  await tasks.verifyModelManager(environmentPython, context());
  fs.rmSync(directPath);
  await tasks.verifyModelManager(environmentPython, context());

  const reused = await tasks.findInstalledModelManager(context());
  assert.equal(reused.environmentRoot, tasks.paths.environmentRoot);
  assert.equal(reused.wheel_sha256, tasks.wheelSha256);

  const wrongWheelTasks = new BootstrapTasks({ dataDirectory: root, wheelPath: tasks.wheelPath,
    wheelSha256: '0'.repeat(64) });
  wrongWheelTasks.environment = tasks.environment;
  wrongWheelTasks.paths = tasks.paths;
  await assert.rejects(wrongWheelTasks.verifyModelManager(environmentPython, context()),
    (error) => error.code === 'MODEL_MANAGER_WHEEL_HASH_MISMATCH');

  const packageFile = path.join(siteRoot, 'speechcraft_models', '__init__.py');
  const originalCode = fs.readFileSync(packageFile);
  try {
    fs.appendFileSync(packageFile, '\n# acceptance-test tamper\n');
    await assert.rejects(tasks.verifyModelManager(environmentPython, context()),
      (error) => error.code === 'MODEL_MANAGER_INSTALLED_FILES_MISMATCH');
    await assert.rejects(tasks.findInstalledModelManager(context()),
      (error) => error.code === 'MODEL_MANAGER_INSTALLED_FILES_MISMATCH');
  } finally { fs.writeFileSync(packageFile, originalCode); }
  await tasks.verifyModelManager(environmentPython, context());

  const metadataPath = path.join(siteRoot, distInfo, 'METADATA');
  const originalMetadata = fs.readFileSync(metadataPath, 'utf8');
  try {
    fs.writeFileSync(metadataPath, originalMetadata.replace(/^Version: 0\.1\.0$/m, 'Version: 9.9.9'));
    await assert.rejects(tasks.verifyModelManager(environmentPython, context()),
      (error) => error.code === 'MODEL_MANAGER_PACKAGE_VERSION_MISMATCH');
  } finally { fs.writeFileSync(metadataPath, originalMetadata); }
  await tasks.verifyModelManager(environmentPython, context());
});

test('manager validation reports distinct catalog and managed-path failures', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-manager-codes-'));
  try {
    const tasks = new BootstrapTasks({ dataDirectory: root, wheelPath: '/unused', wheelSha256: 'a'.repeat(64) });
    tasks.environment = { profile: 'linux-x86_64' };
    tasks.paths = { pythonRoot: root };
    fs.mkdirSync(path.join(root, 'python'), { recursive: true });
    tasks.paths.pythonRoot = path.join(root, 'python');
    const valid = { version: '0.1.0', python: '3.12.15', prefix: root,
      base_prefix: tasks.paths.pythonRoot, catalog_valid: true,
      roles: ['diarization', 'asr', 'speech_detection', 'speaker_purity'], heavy_dependencies: [] };
    assert.throws(() => tasks.validateManagerProbe({ ...valid, catalog_valid: false }, root),
      (error) => error.code === 'MODEL_MANAGER_CATALOG_INVALID');
    assert.throws(() => tasks.validateManagerProbe({ ...valid, roles: ['asr'], catalog_valid: false }, root),
      (error) => error.code === 'MODEL_MANAGER_CATALOG_INVALID');
    assert.throws(() => tasks.validateManagerProbe({ ...valid, prefix: path.join(root, 'wrong') }, root),
      (error) => error.code === 'MODEL_MANAGER_ENVIRONMENT_PATH_MISMATCH');
    assert.throws(() => tasks.validateManagerProbe({ ...valid, base_prefix: path.join(root, 'elsewhere') }, root),
      (error) => error.code === 'MODEL_MANAGER_MANAGED_PYTHON_PATH_MISMATCH');
    assert.throws(() => tasks.validateManagerProbe({ ...valid, version: '9.9.9' }, root),
      (error) => error.code === 'MODEL_MANAGER_PACKAGE_VERSION_MISMATCH');
    assert.throws(() => tasks.validateManagerProbe({ ...valid, failure_code: 'MODEL_MANAGER_INSTALLED_FILES_MISMATCH' }, root),
      (error) => error.code === 'MODEL_MANAGER_INSTALLED_FILES_MISMATCH');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
