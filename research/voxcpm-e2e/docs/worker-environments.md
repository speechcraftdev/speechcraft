# Optional evaluator worker environments

The CLI keeps neural evaluators in separate Python processes. Install optional dependencies in isolated environments and configure their interpreter paths with YAML or environment variables. The core package does not bundle models or weights.

- Set `SPEECHCRAFT_ASR_PYTHON` to a Python environment containing faster-whisper and Silero VAD.
- Set `SPEECHCRAFT_SPEAKER_PYTHON` to an environment containing the configured speaker encoder backend.
- Set the `ttsds2.python` config value to an environment containing `ttsds==2.1.3`.

The source tree includes compatible overlay lock files under `worker-requirements/`. Match each lock to a compatible PyTorch/torchaudio base where needed; do not copy binary packages between environments. Keep evaluator model caches outside this repository.

TTSDS calls the upstream `BenchmarkSuite`; the adapter does not reimplement metric calculations. The primary category weights are equal at 0.25 for Speaker, Prosody, Intelligibility and Generic, with Environment disabled.
