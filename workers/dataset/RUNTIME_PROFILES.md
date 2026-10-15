# Linux processing runtime profiles

SpeechCraft's Node setup controller installs the dataset worker from the trusted recipes in `runtime-profiles/`. The worker code is shared; the lock is selected by runtime profile. Both locks require the controller-managed CPython **3.12.15** and Linux x86-64. They pin PyTorch and torchaudio to the same **2.8.0** release, `pyannote.audio==4.0.3`, `faster-whisper==1.2.1`, `ctranslate2==4.7.2`, `onnxruntime==1.26.0`, `sherpa-onnx==1.13.8`, and `av==18.1.0`. PyAV 18.1 is intentional: PyAV 19 removed an argument currently used by faster-whisper 1.2.1.

| Profile | Torch source | Status in this checkout |
| --- | --- | --- |
| `linux-x86_64-cpu` | Official PyTorch CPU wheel index | Installed and exercised on Linux CPU; see scope and blockers below |
| `linux-x86_64-cuda` | Official PyTorch CUDA 12.8 wheel index (`torch==2.8.0+cu128`, `torchaudio==2.8.0+cu128`) | Recipe is pinned but remains unavailable: CUDA package set and end-to-end CUDA worker profile are not validated |
| `macos-arm64` | None | Unavailable; no validated worker lock |

The runtime manifest records the project and lock SHA-256 digests. The controller rejects any mismatch and checks the managed interpreter and imports after `uv sync --locked`. Do not edit a lock without updating its trusted digest and rerunning validation. CPU and CUDA profiles are separate locks: a resolver override is not used to force incompatible PyTorch/torchaudio constraints. Pyannote 4.0.3 resolved with Torch/Torchaudio 2.8.0 without a compatibility override.

GPU visibility is only a candidate signal. The controller queries `nvidia-smi` for GPU name and driver, but keeps the CPU profile selected unless the trusted CUDA recipe is marked available and validated and separate PyTorch and CTranslate2 inference probes succeed. A Torch tensor test does not prove Whisper/CTranslate2 or pyannote CUDA works. If validation fails, a job must restart using the separately validated CPU environment; an active dataset job never changes device mid-run.

## Paths and offline model loading

The controller owns versioned environments under its persistent `environments/` directory. The model manager's verified artifacts remain under the independent persistent `models/` directory. Backend launch receives model locations and the selected model platform from verified controller component state. In managed mode, the worker requires local Silero and Whisper paths, verifies the catalog inventory and hashes before processing, forces `large-v3`, and enables Hugging Face/Transformers offline behavior. It does not fall back to arbitrary user caches or download weights during processing.

The frozen VR O0_4 slicer continues to use the official Silero ONNX wrapper and its existing thresholds and packing behavior. Managed mode supplies the model-manager ONNX file explicitly; development mode retains the existing `silero-vad` package behavior.

Speaker Purity's development adapter can load a local sherpa-onnx TitaNet-Large ONNX file when the explicit experimental environment flag is set. Its values are tagged uncalibrated and cannot be automatically accepted/rejected with historical NeMo cutoffs. The model's provenance and redistribution blocker remains in force; production setup must not enable that flag.

Community-1 inference requires an authorized local snapshot. No snapshot or gated authorization is included in this change. The worker must report the missing local model rather than substituting a diarizer. FFmpeg/FFprobe paths are explicit in managed mode, but this checkout has no trusted, license-cleared prebuilt artifact recipe; the controller leaves the tool blocked and processing unavailable rather than trusting PATH or requiring apt/Homebrew.

## Runtime probes and validation

`scripts/runtime_probe.py` implements JSON probes for managed FFmpeg/FFprobe, Silero ONNX, local Whisper Large-v3, experimental TitaNet, local Community-1, and CUDA. The setup controller invokes the probes through the trusted worker interpreter, with offline Hugging Face settings and only controller-verified model paths. A local intelligible WAV may be supplied to the controller process as `SPEECHCRAFT_RUNTIME_PROBE_AUDIO`; it is not accepted through the browser API or persisted. Probe failures remain per-component blockers, and do not mark `production_ready`.

On the current Linux x86-64 host, managed CPU-profile Silero inference passed (250 finite frames). Managed CPU-profile Whisper Large-v3 inference passed offline against the verified model (1 segment, nonempty transcript, 15.36 seconds on the probe run; transcript omitted). A CUDA candidate exists (RTX 4060, driver 610.57.04), but CTranslate2 GPU inference was attempted against the verified Large-v3 model and failed because `libcublas.so.12` could not be loaded. The existing worker environment has CPU-only PyTorch. CUDA is therefore **not validated or selectable**. No authorized Community-1 snapshot was available. No managed FFmpeg binary was available, so the real FFmpeg decode probe and full worker run remain blocked. macOS and WSL2 were not executed.

FFmpeg provisioning remains deliberately blocked. FFmpeg upstream explains that optional GPL components change the resulting license and its LGPL distribution checklist calls for exact corresponding source/configuration and dynamic linking (see [FFmpeg legal considerations](https://ffmpeg.org/legal.html)). BtbN publishes a pinned LGPL-labelled Linux build, but it is a third-party static bundle and the available metadata does not establish SpeechCraft's exact redistribution/compliance obligations or provide a verified immutable archive digest suitable for our trusted recipe. SpeechCraft must not promote `/usr/bin/ffmpeg` or download an arbitrary static build. Release work must choose an audited build, pin asset identity and digest, retain matching source/configure materials and notices, and obtain licensing approval before setting `ffmpeg.available` true.

Use these commands for offline worker checks after preparing the locked environment:

```sh
cd workers/dataset
uv sync --locked --project runtime-profiles/linux-cpu
python -m pytest -q
```

For controller installation, use the real setup-controller runtime plan and its trusted `install_worker_environment` stage; do not manually create or activate a global Python environment. To run a real local probe, point the controller process at a short intelligible recording with `SPEECHCRAFT_RUNTIME_PROBE_AUDIO=/absolute/path/to/speech.wav`; use the authenticated setup job and inspect component status. Runtime inference is not declared ready by an environment import probe.

## Release blockers and next runtime work

1. Provision a trusted Linux FFmpeg/FFprobe artifact with a verified digest and accepted redistribution terms; then run the managed decode/probe helper through the controller.
2. Obtain legitimate Community-1 access and a pinned local snapshot, then implement and run actual CPU diarization. Validate CUDA diarization separately if hardware is available.
3. Run and validate the checked-in trusted helpers for Silero, Whisper, TitaNet, Community-1, and FFmpeg with approved local artifacts; currently only Silero and Whisper have passed actual inference on this host.
4. Run a real end-to-end dataset job only after diarization and managed FFmpeg are available. Verify output files and project/export schemas, not only worker exit status.
5. Validate the CUDA profile and actual CTranslate2/PyTorch/pyannote inference on a compatible NVIDIA host. If CUDA execution fails, use CPU only after validating the CPU runtime for the requested operation.
6. Preserve TitaNet's provenance/redistribution blocker until its source checkpoint and license are resolved and score calibration is evaluated.
7. Validate Apple Silicon worker compatibility separately; this Linux lock does not establish MLX or macOS inference support.
