import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { RuntimeTasks } from '../src/runtime-tasks.js';
import { runOwnedProcess } from '../src/bootstrap.js';
import { createSetupController } from '../src/server.js';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const modelManagerRoot = path.join(repositoryRoot, 'packages', 'model_manager');
const python = process.env.SPEECHCRAFT_MODEL_MANAGER_TEST_PYTHON || 'python3';
const payloads = {
  'config.yaml': 'fixture-config\n',
  'embedding/pytorch_model.bin': 'fixture-embedding\n',
  'plda/plda.npz': 'fixture-plda\n',
  'plda/xvec_transform.npz': 'fixture-transform\n',
  'segmentation/pytorch_model.bin': 'fixture-segmentation\n',
};

const pythonShim = (catalog, files, cache) => `
import json, pathlib, sys, types
sys.path.insert(0, ${JSON.stringify(modelManagerRoot)})
import speechcraft_models.cli as cli
import speechcraft_models.download as download
import speechcraft_models.huggingface_source as hf
catalog=json.loads(${JSON.stringify(JSON.stringify(catalog))})
files=json.loads(${JSON.stringify(JSON.stringify(files))})
repo='fixture/pyannote-community-1'
revision='${'a'.repeat(40)}'
paths=frozenset(files)
hf.GATED_REPOSITORIES.clear()
hf.GATED_REPOSITORIES[repo]={'revision':revision,'files':paths,'access_file':'config.yaml'}
download.load_catalog=lambda:catalog
cli.load_catalog=lambda:catalog
hub=types.ModuleType('huggingface_hub')
hub.constants=types.SimpleNamespace(HF_HOME=${JSON.stringify(cache)},HF_HUB_CACHE=${JSON.stringify(cache)},HF_XET_CACHE=${JSON.stringify(cache)})
sys.modules['huggingface_hub']=hub
class Api:
 def whoami(self, *, token):
  assert token == 'hf_SECRET_MARKER_fake_hub_123456'
  return {'name':'fixture-user'}
 def model_info(self, repo_id, *, revision, token, files_metadata):
  assert repo_id == repo and revision == '${'a'.repeat(40)}' and token == 'hf_SECRET_MARKER_fake_hub_123456' and files_metadata
  return types.SimpleNamespace(sha=revision,siblings=[types.SimpleNamespace(rfilename=name) for name in files])
def download_file(*, repo_id, filename, revision, token, local_dir, force_download):
 assert repo_id == repo and filename == 'config.yaml' and revision == '${'a'.repeat(40)}' and token == 'hf_SECRET_MARKER_fake_hub_123456'
 target=pathlib.Path(local_dir)/filename; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(files[filename].encode())
def download_snapshot(*, repo_id, repo_type, revision, token, local_dir, allow_patterns, max_workers, tqdm_class):
 assert repo_id == repo and repo_type == 'model' and revision == '${'a'.repeat(40)}' and token == 'hf_SECRET_MARKER_fake_hub_123456'
 assert set(allow_patterns) == set(files) and max_workers == 1
 for name,data in files.items():
  target=pathlib.Path(local_dir)/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(data.encode())
original=download.install_gated_model
def install(root, role, platform, token, *, progress=None):
 return original(root,role,platform,token,progress=progress,gated_hub_client=(Api(),download_file,download_snapshot))
cli.install_gated_model=install
raise SystemExit(cli.main(sys.argv[1:]))
`;

function waitForListening(app) { return `http://127.0.0.1:${app.port}`; }

 test('Node controller invokes the actual model-manager CLI with private stdin and publishes a fake-Hub verified snapshot', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-model-manager-boundary-'));
  const cache = path.join(directory, 'fake-hf-cache'); fs.mkdirSync(cache);
  const catalog = JSON.parse(fs.readFileSync(path.join(modelManagerRoot, 'speechcraft_models', 'model-catalog.json'), 'utf8'));
  const artifact = catalog.models.find((model) => model.role === 'diarization').artifacts[0];
  artifact.revision = 'a'.repeat(40);
  artifact.source.repo_id = 'fixture/pyannote-community-1';
  artifact.required_files = Object.entries(payloads).map(([name, data]) => ({
    path: name, size_bytes: Buffer.byteLength(data),
    sha256: (awaitHash(data)),
  }));
  const helper = pythonShim(catalog, payloads, cache);
  const hubProgress = [];
  let hubStdout = '';
  const actualPython = spawnSync(python, ['--version'], { encoding: 'utf8' });
  assert.equal(actualPython.status, 0, `Python is required for this boundary test: ${actualPython.stderr}`);
  const runtime = new RuntimeTasks({ dataDirectory: directory, applicationRoot: repositoryRoot,
    processRunner: async (executable, args, options) => {
      const action = args[3]; const role = args[4];
      if (role !== 'diarization') return { code: 0, stdout: JSON.stringify({ status: 'verified', install_ready: true,
        integrity_verified: true, root: path.join(directory, 'models', role), role }), stderr: '' };
      const output = options.onOutput;
      const launched = await runOwnedProcess(executable, ['-c', helper, ...args.slice(3)], { ...options,
        onOutput: (stream, chunk) => {
          if (stream === 'stdout') hubStdout += chunk.toString('utf8');
          else hubProgress.push(chunk.toString('utf8'));
          output?.(stream, chunk);
        } });
      return launched;
    } });
  runtime.environment = { profile: 'linux-x86_64', wsl: false, os: 'linux', architecture: 'x86_64' };
  runtime.paths = { home: directory, temporary: directory, uvCache: directory, pythonRoot: directory,
    hfHome: cache, hfHub: cache, hfXet: cache };
  runtime.getManagerPython = () => python;
  const tasks = { stages: ['install_models'], taskId: (stage) => `runtime:${stage}`,
    initialBootstrapStatus: () => runtime.initialBootstrapStatus(),
    initialProgress: (_stage, message) => ({ phase: 'queued', current: null, total: null, percent: null, message }),
    completionProgress: () => ({ phase: 'complete', current: null, total: null, percent: null, message: 'models checked' }),
    run: (stage, context) => runtime.run(stage, context) };
  let app;
  try {
    app = await createSetupController({ dataDirectory: directory, port: 0, mode: 'runtime', tasks });
    const base = waitForListening(app);
    const bootstrap = fs.readFileSync(app.bootstrapPath, 'utf8').trim();
    const session = await fetch(`${base}/setup-api/session`, { method: 'POST', headers: {
      origin: base, authorization: `Bearer ${bootstrap}`, 'content-type': 'application/json' }, body: '{}' });
    const auth = await session.json();
    const cookie = session.headers.get('set-cookie').split(';', 1)[0];
    const post = (route, body = {}) => fetch(`${base}${route}`, { method: 'POST', headers: {
      origin: base, cookie, 'x-speechcraft-csrf': auth.csrf_token, 'content-type': 'application/json' }, body: JSON.stringify(body) });

    assert.equal((await post('/setup-api/start')).status, 202);
    await app.controller.waitForIdle();
    assert.equal(app.store.snapshot().job.state, 'waiting_for_user');
    const secret = 'hf_SECRET_MARKER_fake_hub_123456';
    const authorized = await post('/setup-api/models/pyannote/authorize', { token: secret });
    assert.equal(authorized.status, 202);
    assert.equal(JSON.stringify(await authorized.json()).includes(secret), false);
    await app.controller.waitForIdle();
    const component = app.store.snapshot().job.bootstrap_status.components.pyannote;
    assert.equal(app.store.snapshot().job.state, 'completed', JSON.stringify({ error: app.store.snapshot().job.error,
      stdout: hubStdout.slice(-4000), progress: hubProgress.join('').slice(-4000) }));
    assert.equal(component.state, 'files_verified');
    assert.equal(component.report.inventory_exact, true);
    assert.equal(component.report.integrity_verified, true);
    assert.equal(fs.existsSync(path.join(component.path, 'config.yaml')), true);
    const transferProgress = hubProgress.join('');
    assert.match(transferProgress, /"state": "checking_token"/);
    assert.match(transferProgress, /"state": "checking_model_access"/);
    assert.match(transferProgress, /"state": "downloading"/);
    const publicBodies = [await fetch(`${base}/setup-api/status`, { headers: { cookie } }).then((r) => r.json()),
      await fetch(`${base}/setup-api/diagnostics`, { headers: { cookie } }).then((r) => r.json())];
    assert.equal(JSON.stringify(publicBodies).includes(secret), false);
    for (const file of filesUnder(directory)) assert.equal(fs.readFileSync(file).includes(secret), false, path.relative(directory, file));

    assert.equal((await post('/setup-api/start')).status, 202);
    await app.controller.waitForIdle();
    assert.equal(app.store.snapshot().job.bootstrap_status.components.pyannote.state, 'files_verified');
    assert.equal(app.store.snapshot().job.state, 'completed');
  } finally {
    await app?.close().catch(() => {});
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

function awaitHash(value) {
  return createHash('sha256').update(value).digest('hex');
}

function filesUnder(root) {
  return fs.readdirSync(root, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(root, entry.name);
    return entry.isDirectory() ? filesUnder(file) : [file];
  });
}
