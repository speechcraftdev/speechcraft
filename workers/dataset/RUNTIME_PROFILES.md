# Linux processing runtime profiles

SpeechCraft's Node setup controller installs the dataset worker from the trusted recipes in `runtime-profiles/`. The worker code is shared; the lock is selected by runtime profile. Both locks require the controller-managed CPython **3.12.15** and Linux x86-64. They pin PyTorch and torchaudio to the same **2.8.0** release, `pyannote.audio==4.0.3`, `faster-whisper==1.2.1`, `ctranslate2==4.7.2`, `onnxruntime==1.26.0`, `sherpa-onnx==1.13.8`, and `av==18.1.0`. PyAV 18.1 is intentional: PyAV 19 removed an argument currently used by faster-whisper 1.2.1.

| Profile | Torch source | Status in this checkout |
| --- | --- | --- |
| `linux-x86_64-cpu` | Official PyTorch CPU wheel index | Installed and exercised on Linux CPU; see scope and blockers below |
| `linux-x86_64-cuda` | Official PyTorch CUDA 12.8 wheel index (`torch==2.8.0+cu128`, `torchaudio==2.8.0+cu128`) | Recipe is pinned, but unavailable until driver and real CUDA inference validation succeed |
| `macos-arm64` | None | Unavailable; no validated worker lock |

The runtime manifest records the project and lock SHA-256 digests. The controller rejects any mismatch and checks the managed interpreter and imports after `uv sync --locked`. Do not edit a lock without updating its trusted digest and rerunning validation. CPU and CUDA profiles are separate locks: a resolver override is not used to force incompatible PyTorch/torchaudio constraints. Pyannote 4.0.3 resolved with Torch/Torchaudio 2.8.0 without a compatibility override. CTranslate2 4.7.2 shares the profile; its CUDA execution still needs a real GPU probe.

## Paths and offline model loading

The controller owns versioned environments under its persistent `environments/` directory. The model manager's verified artifacts remain under the independent persistent `models/` directory. Backend launch receives model locations and the selected model platform from verified controller component state. In managed mode, the worker requires local Silero and Whisper paths, verifies the catalog inventory and hashes before processing, forces `large-v3`, and enables Hugging Face/Transformers offline behavior. It does not fall back to arbitrary user caches or download weights during processing.

The frozen VR O0_4 slicer continues to use the official Silero ONNX wrapper and its existing thresholds and packing behavior. Managed mode supplies the model-manager ONNX file explicitly; development mode retains the existing `silero-vad` package behavior.

Speaker Purity's development adapter can load a local sherpa-onnx TitaNet-Large ONNX file when the explicit experimental environment flag is set. Its values are tagged uncalibrated and cannot be automatically accepted/rejected with historical NeMo cutoffs. The model's provenance and redistribution blocker remains in force; production setup must not enable that flag.

Community-1 inference requires an authorized local snapshot. No snapshot or gated authorization is included in this change. The worker must report the missing local model rather than substituting a diarizer. FFmpeg/FFprobe paths are explicit in managed mode, but this checkout has no trusted, license-cleared prebuilt artifact recipe; the controller leaves the tool blocked and processing unavailable rather than trusting PATH or requiring apt/Homebrew.

## Validation performed

The CPU lock was installed in an isolated environment using managed CPython 3.12.15 and actual Silero, local Whisper Large-v3 CPU, and experimental local TitaNet inference were exercised on Linux. The current host has no usable NVIDIA driver, so the CUDA recipe and CUDA execution are not validated. No authorized Community-1 snapshot or managed FFmpeg binary was available, so a complete multi-speaker worker job could not be truthfully reported as successful. macOS and WSL2 were not executed.

Use these commands for offline worker checks after preparing the locked environment:

```sh
cd workers/dataset
uv sync --locked --project runtime-profiles/linux-cpu
python -m pytest -q
```

For controller installation, use the real setup-controller runtime plan and its trusted `install_worker_environment` stage; do not manually create or activate a global Python environment. To test actual inference, provide a controller-verified model root and a representative local audio input. Runtime inference is not declared ready by the worker import probe.

## Release blockers and next runtime work

1. Provision a trusted Linux FFmpeg/FFprobe artifact with a verified digest and acceptable redistribution terms; then probe decoding and probing through those exact paths.
2. Obtain legitimate Community-1 access and a pinned local snapshot, then implement and run actual CPU diarization. Validate CUDA diarization separately if hardware is available.
3. Add trusted inference helpers to the controller manifest for Silero, Whisper, sherpa TitaNet, and Community-1. The controller's current runtime-validation recipe remains blocked because no committed, release-validated probe suite is configured.
4. Run a real end-to-end dataset job only after diarization and managed FFmpeg are available. Verify output files and project/export schemas, not only worker exit status.
5. Validate the CUDA profile and actual CTranslate2/PyTorch/pyannote inference on a compatible NVIDIA host. If CUDA execution fails, use CPU only after validating the CPU runtime for the requested operation.
6. Preserve TitaNet's provenance/redistribution blocker until its source checkpoint and license are resolved and score calibration is evaluated.
7. Validate Apple Silicon worker compatibility separately; this Linux lock does not establish MLX or macOS inference support.
