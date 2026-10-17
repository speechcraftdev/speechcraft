import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { hostEnvironment } from './platform.js';

const runtimeManifestPath = fileURLToPath(new URL('../resources/runtime-manifest.json', import.meta.url));
export const runtimeManifest = JSON.parse(fs.readFileSync(runtimeManifestPath, 'utf8'));

export const PRODUCTION_STAGES = Object.freeze([
  'inspect_system', 'install_uv', 'install_python', 'install_model_manager', 'validate_bootstrap',
  'install_backend_environment', 'install_worker_environment', 'install_models',
  'validate_runtime', 'start_services', 'verify_services',
]);

export function cudaProfileReady(recipe, gpuDetected, cudaProbe) {
  return gpuDetected === true && recipe?.available === true
    && cudaProbe?.schema_version === 1 && cudaProbe?.component === 'cuda'
    && cudaProbe?.success === true && cudaProbe?.inference_completed === true
    && cudaProbe?.torch_cuda_success === true && cudaProbe?.ctranslate2_cuda_success === true
    && cudaProbe?.gpu_detected === true && typeof cudaProbe?.driver_identification === 'string'
    && Boolean(cudaProbe.driver_identification.trim())
    && typeof cudaProbe?.actual_execution_device === 'string' && Boolean(cudaProbe.actual_execution_device.trim())
    && ['cublas', 'cudnn', 'cudart'].every((name) => cudaProbe?.required_libraries?.[name] === true)
    && cudaProbe?.measurements?.whisper_segments > 0;
}

export function expectedWorkerMachines(workerProfile) {
  if (workerProfile === 'macos-arm64') return Object.freeze(['aarch64', 'arm64']);
  if (String(workerProfile).startsWith('linux-x86_64')) return Object.freeze(['amd64', 'x86_64']);
  return Object.freeze([]);
}

export function selectRuntimeProfile(host = hostEnvironment(), capabilities = {}) {
  if (host.profile === 'macos-arm64') return { host, profile: 'macos-arm64', modelPlatform: 'macos-arm64', workerProfile: 'macos-arm64' };
  if (host.profile === 'linux-x86_64') {
    const cudaValidated = capabilities.cudaValidated === true
      && cudaProfileReady(runtimeManifest.worker.profiles['linux-x86_64-cuda'], capabilities.gpuDetected, capabilities.cudaProbe);
    const workerProfile = cudaValidated ? 'linux-x86_64-cuda' : 'linux-x86_64-cpu';
    return { host, profile: 'linux-x86_64', modelPlatform: host.wsl ? 'windows-wsl2' : 'linux-x86_64', workerProfile,
      accelerator: cudaValidated ? 'cuda' : 'cpu', cuda_candidate: capabilities.gpuDetected === true,
      cuda_validated: cudaValidated, cpu_fallback_available: runtimeManifest.worker.profiles['linux-x86_64-cpu'].available === true
        && runtimeManifest.worker.profiles['linux-x86_64-cpu'].validated === true };
  }
  throw Object.assign(new Error('No trusted SpeechCraft runtime profile exists for this host.'), { code: 'RUNTIME_PROFILE_UNAVAILABLE' });
}

const TECHNICAL_COMPONENTS = Object.freeze([
  ['managed_python', 'verified'], ['backend_environment', 'verified'], ['worker_environment', 'verified'],
  ['ffmpeg', 'runtime_ready'], ['silero', 'runtime_ready'], ['whisper', 'runtime_ready'],
  ['pyannote', 'runtime_ready'], ['titanet', 'runtime_ready'], ['backend_service', 'verified'],
]);

/** Single source of truth for technical processing and release readiness. */
export function evaluateRuntimeReadiness(components = {}) {
  const blockers = [];
  for (const [name, expected] of TECHNICAL_COMPONENTS) {
    const value = components[name];
    const ready = expected === 'verified'
      ? value?.state === 'verified' && (name !== 'backend_service' || value.verification === 'internal_health_identity')
      : value?.state === 'runtime_ready' && value.runtime_ready === true;
    if (!ready) blockers.push({ component: name, code: value?.failure_code || `${name.toUpperCase()}_NOT_READY`,
      message: value?.message || `${name} has not passed its required verification.` });
  }
  const technicalReady = blockers.length === 0;
  const releaseBlockers = [];
  if (components.titanet?.release_approved !== true) releaseBlockers.push({ component: 'titanet',
    code: 'TITANET_PROVENANCE_UNRESOLVED', message: components.titanet?.message || 'TitaNet checkpoint provenance and redistribution approval remain unresolved.' });
  if (components.ffmpeg?.redistribution_approved !== true) releaseBlockers.push({ component: 'ffmpeg',
    code: 'FFMPEG_RELEASE_REVIEW_REQUIRED', message: 'FFmpeg product redistribution compliance review remains required.' });
  if (components.pyannote?.state !== 'runtime_ready') releaseBlockers.push({ component: 'pyannote',
    code: components.pyannote?.failure_code || 'PYANNOTE_RUNTIME_NOT_READY',
    message: components.pyannote?.message || 'Community-1 authorization and offline inference validation are required.' });
  return { technical_ready: technicalReady, production_ready: technicalReady && releaseBlockers.length === 0,
    blockers, release_blockers: releaseBlockers };
}

/** CUDA is selectable only after both the recipe and runtime probes are trusted. */
export function hashMatches(file, expected) {
  if (!/^[a-f0-9]{64}$/.test(expected || '')) return false;
  try {
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.isSymbolicLink()) return false;
    return createHash('sha256').update(fs.readFileSync(file)).digest('hex') === expected;
  } catch { return false; }
}

export function initialComponents() {
  const component = (state, identity = null, failure = null) => ({
    identity, state, verification: state === 'verified' ? 'verified' : 'pending',
    last_verified_at: null, failure_code: failure?.code || null, message: failure?.message || null,
  });
  return {
    uv: component('pending', 'uv 0.12.23'), managed_python: component('pending', 'CPython 3.12.15'),
    model_manager: component('pending', 'speechcraft-model-manager 0.1.0'),
    backend_environment: component('pending', 'speechcraft-backend + backend/uv.lock'),
    worker_environment: runtimeManifest.worker.profiles['linux-x86_64-cpu'].available === true
      ? component('pending', `${runtimeManifest.worker.distribution}/linux-x86_64-cpu locked recipe`)
      : component('blocked', null, { code: 'runtime_profile_unavailable', message: runtimeManifest.worker.profiles['linux-x86_64-cpu'].reason }),
    ffmpeg: runtimeManifest.ffmpeg?.available === true
      ? component('pending', `FFmpeg ${runtimeManifest.ffmpeg.version}; pinned LGPL build`)
      : component('blocked', null, { code: 'runtime_tool_unavailable', message: runtimeManifest.ffmpeg.reason }),
    silero: component('pending', 'manifest-pinned Silero ONNX'),
    whisper: component('pending', 'Whisper Large-v3; platform-selected format'),
    titanet: component('blocked', null, { code: 'model_release_blocked', message: runtimeManifest.models.blocked_roles.speaker_purity }),
    pyannote: component('waiting_for_token', 'pyannote/speaker-diarization-community-1',
      { code: 'HF_MODEL_ACCESS_REQUIRED', message: 'Accept the Community-1 conditions and submit a Hugging Face read token to install this model.' }),
    runtime_probes: runtimeManifest.runtime_validation.available === true
      ? component('pending', 'trusted managed-worker inference probes')
      : component('blocked', null, { code: 'runtime_validation_pending', message: runtimeManifest.runtime_validation.reason }),
    backend_service: component('blocked', null, { code: 'service_start_pending', message: 'Backend service supervision is not configured for this application release.' }),
    dataset_processing: component('blocked', null, { code: 'runtime_profile_unavailable', message: 'The worker environment and required FFmpeg runtime are not verified.' }),
  };
}

export function runtimeRoots(dataDirectory) {
  return {
    models: path.join(dataDirectory, 'models'),
    environments: path.join(dataDirectory, 'environments'),
    caches: path.join(dataDirectory, 'cache'),
  };
}

export function assertTrustedBackendRecipe(projectRoot) {
  const project = path.join(projectRoot, 'backend');
  const lock = path.join(project, 'uv.lock');
  const manifest = path.join(project, 'pyproject.toml');
  if (!hashMatches(lock, runtimeManifest.backend.lock_sha256)
      || !hashMatches(manifest, runtimeManifest.backend.project_manifest_sha256)) {
    throw Object.assign(new Error('The backend lockfile is missing or does not match the trusted runtime recipe.'), { code: 'BACKEND_RECIPE_INTEGRITY_FAILED' });
  }
  return { project, lock, manifest };
}
