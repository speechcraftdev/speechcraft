import assert from 'node:assert/strict';
import test from 'node:test';
import { cudaProfileReady, expectedWorkerMachines, runtimeManifest, selectRuntimeProfile } from '../src/runtime-plan.js';

// These cases simulate hardware and probe reports. They are not GPU acceptance.
const wslHost = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true };
const passingCuda = {
  schema_version: 1, component: 'cuda', success: true, inference_completed: true,
  gpu_detected: true, driver_identification: 'simulated WSL NVIDIA driver', actual_execution_device: 'simulated GPU',
  required_libraries: { cublas: true, cudnn: true, cudart: true },
  torch_cuda_success: true, ctranslate2_cuda_success: true, measurements: { whisper_segments: 1 },
};

test('simulated WSL2 runtime selection stays on the Linux CPU profile without a GPU', () => {
  const selected = selectRuntimeProfile(wslHost, { gpuDetected: false });
  assert.equal(selected.modelPlatform, 'windows-wsl2');
  assert.equal(selected.workerProfile, 'linux-x86_64-cpu');
  assert.equal(selected.accelerator, 'cpu');
  assert.equal(selected.cuda_validated, false);
  assert.equal(selected.cpu_fallback_available, true);
});

test('a simulated WSL-visible GPU stays on CPU until the CUDA profile is actually compatible', () => {
  const visible = selectRuntimeProfile(wslHost, { gpuDetected: true });
  assert.equal(visible.cuda_candidate, true);
  assert.equal(visible.workerProfile, 'linux-x86_64-cpu');
  assert.equal(visible.modelPlatform, 'windows-wsl2');
  const incompatible = selectRuntimeProfile(wslHost, {
    gpuDetected: true, cudaValidated: true,
    cudaProbe: { ...passingCuda, driver_identification: '', success: false, inference_completed: false },
  });
  assert.equal(incompatible.workerProfile, 'linux-x86_64-cpu');
  assert.equal(incompatible.modelPlatform, 'windows-wsl2');
  assert.equal(cudaProfileReady(runtimeManifest.worker.profiles['linux-x86_64-cuda'], true,
    { ...passingCuda, success: false, inference_completed: false, driver_identification: '' }), false);
});

test('simulated Torch CUDA success with CTranslate2 failure falls back to CPU', () => {
  const torchOnly = { ...passingCuda, ctranslate2_cuda_success: false };
  assert.equal(cudaProfileReady(runtimeManifest.worker.profiles['linux-x86_64-cuda'], true, torchOnly), false);
  const selected = selectRuntimeProfile(wslHost, { gpuDetected: true, cudaValidated: true, cudaProbe: torchOnly });
  assert.equal(selected.workerProfile, 'linux-x86_64-cpu');
  assert.equal(selected.accelerator, 'cpu');
  assert.equal(selected.modelPlatform, 'windows-wsl2');
});

test('a simulated compatible WSL CUDA probe can select the existing Linux CUDA profile', () => {
  const selected = selectRuntimeProfile(wslHost, { gpuDetected: true, cudaValidated: true, cudaProbe: passingCuda });
  assert.equal(selected.workerProfile, 'linux-x86_64-cuda');
  assert.equal(selected.accelerator, 'cuda');
  assert.equal(selected.cuda_validated, true);
  assert.equal(selected.modelPlatform, 'windows-wsl2');
  assert.deepEqual(selectRuntimeProfile(wslHost, { gpuDetected: true, cudaValidated: true, cudaProbe: passingCuda }), selected);
});

test('WSL and Linux worker profiles reject an ARM64 interpreter', () => {
  assert.deepEqual(expectedWorkerMachines('linux-x86_64-cpu'), ['amd64', 'x86_64']);
  assert.deepEqual(expectedWorkerMachines('linux-x86_64-cuda'), ['amd64', 'x86_64']);
  assert.deepEqual(expectedWorkerMachines('macos-arm64'), ['aarch64', 'arm64']);
  assert.equal(expectedWorkerMachines('linux-x86_64-cpu').includes('arm64'), false);
});

test('missing CUDA libraries in a simulated probe do not count as a compatible profile', () => {
  const missing = { ...passingCuda, required_libraries: { cublas: true, cudnn: false, cudart: true } };
  assert.equal(cudaProfileReady(runtimeManifest.worker.profiles['linux-x86_64-cuda'], true, missing), false);
  assert.equal(selectRuntimeProfile(wslHost, { gpuDetected: true, cudaValidated: true, cudaProbe: missing }).workerProfile, 'linux-x86_64-cpu');
});
