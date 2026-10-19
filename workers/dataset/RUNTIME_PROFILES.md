# Linux processing runtime profiles

SpeechCraft's Node setup controller owns versioned Python environments, tools and model locations. It installs these from trusted lockfiles and runs inference probes in the selected managed worker interpreter. Runtime support is component-specific: installed files, a passing inference probe, and application production readiness are separate states.

## Linux profiles

| Profile | Recipe | Status |
| --- | --- | --- |
| `linux-x86_64-cpu` | CPython 3.12.15; `runtime-profiles/linux-cpu/uv.lock` | Installed and exercised on Linux CPU. Silero, Whisper Large-v3, and a genuine WeSpeaker ECAPA-TDNN512-LM embedding probe passed. The post-diarization worker flow passed with explicitly test-only reference speaker regions. |
| `linux-x86_64-cuda` | CPython 3.12.15; `runtime-profiles/linux-cuda/uv.lock`; PyTorch/torchaudio 2.8.0 CUDA 12.8 | Validated on the development host's NVIDIA GeForce RTX 4060 Laptop GPU with driver 610.57.04. Controller installs it only as a candidate and selects it only after the torch CUDA operation, required library checks, CTranslate2 discovery, and actual local Large-v3 CUDA transcription all pass. Other hardware is revalidated during setup. |
| `macos-arm64` | No validated worker lock | Unavailable; Apple Silicon runtime testing remains Kavin's follow-up. |

The CPU and CUDA locks pin PyTorch and torchaudio together at 2.8.0, pyannote.audio 4.0.3, faster-whisper 1.2.1, CTranslate2 4.7.2, ONNX Runtime 1.26.0, sherpa-onnx 1.13.8 and PyAV 18.1.0. PyAV 18.1 is intentional because faster-whisper 1.2.1 uses an API removed in PyAV 19. Pyannote 4.0.3 resolves against Torch/Torchaudio 2.8.0 without a compatibility override. Recipe lock and project digests are pinned in `setup-controller/resources/runtime-manifest.json`; the controller refuses a changed file.

CUDA is never selected from GPU visibility alone. The setup probe separately records driver identity, Torch execution, CUDA library availability and a real CTranslate2/Whisper inference. This host's managed CUDA candidate passed with Torch 2.8.0+cu128, torchaudio 2.8.0+cu128, CUDA runtime 12.8, CTranslate2 4.7.2, one detected CTranslate2 CUDA device, and a three-segment/283-character transcription from the pinned local Large-v3 model using float16 in 6.14 seconds. The previously observed missing `libcublas.so.12` was resolved by the pinned CUDA wheel dependencies in the managed environment and a controlled `LD_LIBRARY_PATH` assembled only from that environment's `nvidia/*/lib` packages; no system CUDA installation or symlink was used. If a future report is incomplete or fails, the already validated CPU environment remains selected; an active dataset job does not switch devices.

The controller's current free-space estimates are 10 GiB for the CPU worker environment and 16 GiB for the CUDA candidate, each plus the 512 MiB safety margin. The model manager separately estimates model transfers/cache space; the pinned Whisper Large-v3 files occupy about 3.1 GB after installation. The FFmpeg archive is about 131 MiB and its two installed executables together are about 271 MiB. These are installation estimates rather than a promise about dataset scratch-space needs. No general minimum RAM figure is claimed; actual inference resource limits must be established on supported deployment hardware.

## FFmpeg and FFprobe

The Linux x86-64 runtime recipe pins BtbN release `autobuild-2026-10-14-13-03`, artifact SHA-256 `602ca2985139dbab87e0d7c70f48afc69548060463dcfab44c0e5f50ebe39df8`, and matching executable hashes in the trusted manifest. The selected build identifies FFmpeg source revision `46d899d7a02bc63237472cc4897258d5ddf274ac`, reports LGPL-3.0-or-later with `--enable-version3`, and has no `--enable-gpl`/`--enable-nonfree` flags. The controller streams and verifies the archive, extracts only the paired binaries plus the license text into a versioned managed tools directory, checks both binary hashes/version identity, then runs the audio decode probe. Managed workers resolve the explicit `SPEECHCRAFT_FFMPEG_BIN` and `SPEECHCRAFT_FFPROBE_BIN`; they do not fall back to PATH.

The archive was installed into a fresh isolated data root and the pinned pair decoded the bundled 18-second MP3 to 16 kHz mono PCM: 288,000 samples, 18.0 seconds. The `ffprobe` duration and decoded duration matched. Reuse is covered by offline controller tests. See [`FFMPEG-LICENSE-NOTICE.md`](../../setup-controller/resources/FFMPEG-LICENSE-NOTICE.md) for the source/build references and remaining product legal review. Artifact integrity and local execution do not themselves grant redistribution permission or resolve codec patent questions.

## Models, paths and offline processing

The model manager's verified files are authoritative under the persistent SpeechCraft model root, separate from versioned application and Python environments. The controller supplies verified local Whisper Large-v3 CTranslate2 and Silero ONNX paths. The worker verifies their inventory and hashes, forces Large-v3, and uses the same local Whisper path for transcription and Transcript Correctness. Managed mode sets Hugging Face offline flags; it does not search user caches or download during dataset processing. FFmpeg and FFprobe are resolved through the controller-provided executable paths.

The frozen VR O0_4 slicer retains its existing Silero ONNX wrapper, thresholds, segmentation and packing math. In managed mode it loads the model manager's ONNX file directly. It does not add a new VAD pass to the production pipeline. WeSpeaker ECAPA runs offline through the pinned ONNX Runtime CPU path with the benchmark-matched WeSpeaker Kaldi fbank frontend. Its 192-dimensional embeddings are hash-verified, finite and nonzero. Speaker Purity outputs retain raw continuous cosine similarities and an explicit `uncalibrated_review_required` status; no fixed threshold produces pass/fail decisions.

The controller supports the legitimate gated flow for Community-1: accept the
current Hugging Face conditions, submit a read token through the authenticated
setup API, verify token identity and repository access separately, then install
the pinned snapshot through the model manager. The token is ephemeral and is
never stored or used during inference. The included two-voice CC0 synthetic
fixture is a pinned, CC0 two-voice audio recording. The probe runs actual
Community-1 inference and converts its pyannote `Annotation` through the
production `annotation_to_speechcraft_regions` contract, requiring exactly two
speaker IDs and finite, ordered, in-recording regions. It does not establish
quality on human multi-speaker speech. The real gated inference smoke remains
not executed when an authorized snapshot is unavailable. No diarization
fallback is used. Full production processing remains blocked until an
authorized snapshot has passed the offline probe and other release blockers
are resolved.

## Probe fixtures and protocol

The controller's trusted `speechcraft-runtime-probe-v1` recipes invoke `scripts/runtime_probe.py` with the worker or candidate CUDA interpreter. The small checked-in CC0 fixtures are pinned by both the runtime catalog and helper integrity checks: `speech.wav` for speech-positive Whisper/ECAPA inference, `speech-silence.wav` for speech and silence Silero behavior, and `speech-compressed.mp3` for FFmpeg decode. Attribution, derivative details and hashes are in [`runtime-fixtures/README.md`](runtime-fixtures/README.md). Tests and normal installation do not fetch fixture audio.

Probe output is JSON with schema version, component/model identity, backend, device, duration, output measurements, and a safe error code/message. FFmpeg decode, Silero positive speech response, Whisper transcript, WeSpeaker ECAPA 192-dimensional embedding and CUDA Large-v3 inference are genuine operations; a successful import is not enough. Pyannote reports a missing/unauthorized local snapshot as a blocker.

## Reproduce checks

Offline controller tests:

```sh
cd setup-controller
npm test
```

Managed worker tests use the managed worker's locked runtime packages plus an isolated pytest runner (pytest is test tooling, not a production worker dependency):

```sh
WORKER_ENV=/absolute/path/to/managed/worker/environment
TEST_TOOLS=/absolute/path/to/isolated/pytest/environment
PYTHONPATH="$PWD/workers/dataset:$WORKER_ENV/lib/python3.12/site-packages" \
PYTHONNOUSERSITE=1 "$TEST_TOOLS/bin/python" -m pytest -q workers/dataset/tests
```

The real post-diarization smoke uses only the checked-in CC0 fixture and injects an explicitly labelled speaker interval through a test-only harness. It runs actual FFmpeg normalization, local Silero O0_4 slicing, Whisper transcription and TC, WeSpeaker ECAPA/SP/QC, and canonical export. It does not run or claim production diarization:

```sh
SPEECHCRAFT_TEST_ONLY_REFERENCE_REGIONS=1 \
SPEECHCRAFT_MANAGED_RUNTIME=1 \
SPEECHCRAFT_MODEL_PLATFORM=linux-x86_64 \
SPEECHCRAFT_MODEL_ROOT=/absolute/verified/model/root \
SPEECHCRAFT_ASR_MODEL_PATH=/absolute/verified/whisper/path \
SPEECHCRAFT_SILERO_MODEL_DIR=/absolute/verified/silero/model/directory \
SPEECHCRAFT_SILERO_ONNX_PATH=/absolute/verified/silero/path/silero_vad.onnx \
SPEECHCRAFT_ECAPA_ONNX_PATH=/local/verified/voxceleb_ECAPA512_LM.onnx \
SPEECHCRAFT_FFMPEG_BIN=/absolute/managed/tools/ffmpeg \
SPEECHCRAFT_FFPROBE_BIN=/absolute/managed/tools/ffprobe \
SPEECHCRAFT_ASR_DEVICE=cpu HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 \
/absolute/managed/worker/bin/python -I workers/dataset/scripts/downstream_reference_regions_smoke.py \
  --run-root /tmp/speechcraft-reference-regions-smoke
```

Use a fresh isolated run root. The script refuses to execute unless both the test-only annotation flag and managed-runtime mode are set. Do not route its reference annotations into production jobs.

## Current validation and blockers

- **Linux CPU:** managed CPU worker environment was already installed in a separate SpeechCraft-managed runtime root and used for the real probes and downstream smoke. Silero and Whisper probes passed; WeSpeaker ECAPA produced a finite normalized 192-dimensional embedding through offline ONNX Runtime. The downstream worker produced candidate clips, finite TC/SP values, QC artifacts and export files using test-only annotated regions.
- **Managed FFmpeg:** a clean versioned install from the pinned archive completed in an isolated root. Both binaries passed archive/executable digest and version checks; actual fixture decode passed. No system FFmpeg was used.
- **CUDA:** validated on this Linux host with an NVIDIA RTX 4060 Laptop GPU and driver 610.57.04. The actual controller runtime probe passed PyTorch CUDA execution, cuBLAS/cuDNN/cuDART discovery, CTranslate2 CUDA discovery, and local Large-v3 transcription. This evidence covers this hardware/driver combination; other GPU combinations are checked dynamically. Native WSL2 and macOS are not tested.
- **Pyannote:** no authorized Community-1 snapshot is available in this environment. CPU/CUDA Community-1 inference and a genuine pyannote-first full dataset run are not validated.
- **Release:** FFmpeg product legal review remains a separate public-release blocker; ECAPA provenance is documented in the model catalog. Therefore `production_ready` remains false even when the tested CPU/downstream components pass.
- **Platforms:** Linux x86-64 CPU and CUDA were exercised here. WSL2 filesystem policy tests are simulated unit tests; native WSL and macOS were not tested.
