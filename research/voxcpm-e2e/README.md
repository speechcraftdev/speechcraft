# Speechcraft VoxCPM E2E Research Framework

This directory packages the reusable Speechcraft end-to-end TTS evaluation toolkit, model training/inference adapters, worker interfaces, and blind-listening application. It does not contain datasets, generated audio, model weights, worker environments, private result runs, or listening responses.

## Layout

- `src/speechcraft_e2e_bench/` — YAML dataset loading, audio handling, evaluator orchestration, metrics aggregation, failure classification, CLI, reporting, and caches.
- `workers/` — isolated ASR/VAD, speaker encoder, and official TTSDS2 worker processes.
- `workflows/` — VoxCPM training/checkpoint selection, generation orchestration, F5-TTS optional adapters, and a blind listening player.
- `tests/` — deterministic unit tests with synthetic fixtures.
- `config.example.yaml`, `dataset.example.yaml` — starter templates.
- `worker-requirements/` — evaluator dependency overlay pins.

## Install and run evaluator

Python 3.11 or 3.12 is required. From this directory, create an isolated environment and install the core package:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/speechcraft-e2e doctor
.venv/bin/speechcraft-e2e validate dataset.yaml
.venv/bin/speechcraft-e2e evaluate dataset.yaml
.venv/bin/speechcraft-e2e evaluate dataset.yaml --metrics secs,consistency,wer
```

Configure dataset paths in a YAML manifest based on `dataset.example.yaml`. Configure ASR, speaker-encoder, and TTSDS2 worker Python paths in `config.example.yaml` or with `SPEECHCRAFT_ASR_PYTHON` and `SPEECHCRAFT_SPEAKER_PYTHON`. Install optional neural dependencies and checkpoints in isolated environments; none are bundled. The CLI writes run-scoped reports to `runs/` by default.

## Training and inference

The workflow adapters are under `workflows/`. They call separately installed upstream model repositories and require local base checkpoints, datasets, and a run plan. See `workflows/README.md`; this handoff does not include or fetch those dependencies.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src workers workflows tests
```

## Platform compatibility

This research framework was primarily developed and tested on Linux with NVIDIA CUDA. Apple Silicon and macOS execution have not been fully validated. Platform-specific adaptations may be required.
