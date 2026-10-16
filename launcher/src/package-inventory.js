import fs from 'node:fs';
import path from 'node:path';

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

  return { ok: missing.length === 0, missing: [...new Set(missing)], error: missing.length ? 'Required runtime resource inventory is incomplete.' : null };
}
