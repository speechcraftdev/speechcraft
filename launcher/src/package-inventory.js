import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

// These are the concrete entrypoints used by runtime manifests and backend
// subprocess calls. Their parent package trees are included separately by the
// release builder; keeping entrypoints explicit makes omissions fail closed.
export const REQUIRED_RUNTIME_FILES = Object.freeze([
  'setup-controller/src/server.js',
  'setup-controller/resources/runtime-manifest.json',
  'setup-controller/resources/bootstrap-manifest.json',
  'setup-controller/resources/model-manager-requirements.txt',
  'backend/app/main.py',
  'backend/app/dataset_runs.py',
  'backend/app/dataset_worker_client.py',
  'backend/pyproject.toml',
  'backend/uv.lock',
  'workers/dataset/pyproject.toml',
  'workers/dataset/uv.lock',
  'workers/dataset/scripts/preflight.py',
  'workers/dataset/scripts/runtime_probe.py',
  'workers/dataset/speechcraft_dataset/__init__.py',
  'workers/dataset/runtime-fixtures/speech.wav',
  'workers/dataset/runtime-fixtures/speech-silence.wav',
  'workers/dataset/runtime-fixtures/speech-compressed.mp3',
  'workers/dataset/runtime-fixtures/community1-two-voice.wav',
  'workers/dataset/runtime-profiles/linux-cpu/pyproject.toml',
  'workers/dataset/runtime-profiles/linux-cpu/uv.lock',
  'workers/dataset/runtime-profiles/macos-arm64/pyproject.toml',
  'workers/dataset/runtime-profiles/macos-arm64/uv.lock',
  'frontend-release-manifest.json',
  // Linux worker profiles resolve the model manager as a local path dependency
  // while building the worker environment. The standalone model-manager wheel
  // is also packaged for controller bootstrap, but does not replace this
  // source project needed by uv when it synchronizes the worker lock.
  'packages/model_manager/pyproject.toml',
  'packages/model_manager/README.md',
  'packages/model_manager/speechcraft_models/__init__.py',
  'packages/model_manager/speechcraft_models/__main__.py',
  'packages/model_manager/speechcraft_models/cli.py',
  'packages/model_manager/speechcraft_models/download.py',
  'packages/model_manager/speechcraft_models/huggingface_source.py',
  'packages/model_manager/speechcraft_models/manifest.py',
  'packages/model_manager/speechcraft_models/model-catalog.json',
  'packages/model_manager/speechcraft_models/model-manifest.schema.json',
  'workers/dataset/model-catalog.json',
  'workers/dataset/model-manifest.schema.json',
]);

export function validateRuntimeInventory(applicationRoot) {
  const missing = REQUIRED_RUNTIME_FILES.filter((relative) => {
    const filename = path.join(applicationRoot, relative);
    try { return !fs.statSync(filename).isFile(); } catch { return true; }
  });

  const runtimeManifestPath = path.join(applicationRoot, 'setup-controller/resources/runtime-manifest.json');
  let manifest;
  try { manifest = JSON.parse(fs.readFileSync(runtimeManifestPath, 'utf8')); }
  catch (error) { return { ok: false, missing: [...missing, 'setup-controller/resources/runtime-manifest.json'], error: `runtime manifest is invalid: ${error.message}` }; }

  const manifestReferences = [manifest.worker?.validation_helper,
    ...(manifest.runtime_validation?.probes || []).map((probe) => probe.helper),
    ...Object.values(manifest.runtime_validation?.fixtures || {}).map((fixture) => fixture.path)];
  for (const relative of manifestReferences) {
    if (typeof relative !== 'string' || path.isAbsolute(relative) || relative.split(/[\\/]/).includes('..')) {
      missing.push(`invalid-runtime-reference:${String(relative)}`);
      continue;
    }
    try { if (!fs.statSync(path.join(applicationRoot, relative)).isFile()) missing.push(relative); }
    catch { missing.push(relative); }
  }

  try {
    const frontend = JSON.parse(fs.readFileSync(path.join(applicationRoot, 'frontend-release-manifest.json'), 'utf8'));
    if (frontend.schema_version !== 1 || typeof frontend.next_build_id !== 'string' || !Array.isArray(frontend.files)) throw new Error('invalid frontend identity');
    const listed = new Set();
    for (const record of frontend.files) {
      if (!record || typeof record.path !== 'string' || !record.path.startsWith('frontend/') || path.isAbsolute(record.path)
          || record.path.split('/').includes('..') || listed.has(record.path) || !/^[a-f0-9]{64}$/.test(record.sha256 || '')
          || !Number.isSafeInteger(record.size_bytes)) throw new Error('invalid frontend file identity');
      listed.add(record.path);
      const filename = path.join(applicationRoot, record.path);
      const info = fs.lstatSync(filename);
      if (!info.isFile() || info.isSymbolicLink() || info.size !== record.size_bytes
          || createHash('sha256').update(fs.readFileSync(filename)).digest('hex') !== record.sha256) throw new Error(`frontend file identity mismatch: ${record.path}`);
    }
  } catch (error) {
    missing.push(`frontend-release-manifest-invalid:${error.message}`);
  }

  return { ok: missing.length === 0, missing: [...new Set(missing)], error: missing.length ? 'Required runtime resource inventory is incomplete.' : null };
}
