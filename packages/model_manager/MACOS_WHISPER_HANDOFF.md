# Kavin: native Apple Silicon Whisper file smoke

This handoff validates acquisition, file integrity, reuse, and interrupted
transfer recovery only. It does not test MLX inference, GPU execution, TC
scoring, or a CPU fallback. Linux results do not establish macOS behavior.

## Prerequisites

- Apple Silicon Mac (M4 Pro requested), macOS, Python 3.12, and `uv`.
- Network access to public Hugging Face repositories. No Hugging Face token is
  needed or read by this test.
- At least 3.1 GB free for the 3,083,520,685-byte pinned MLX model plus small
  filesystem overhead when the Xet chunk cache is disabled (the `hf-xet` default).
  If `HF_XET_CHUNK_CACHE_SIZE_BYTES` is enabled, allow for the additional cache
  growth reported in the `capacity` progress event. Leave extra working space.
- Keep the test store and cache under `/tmp` or another local macOS filesystem,
  separate from SpeechCraft projects and personal model caches.

## Install the lightweight helper

Run from the SpeechCraft checkout:

```bash
sw_vers
uname -m
uv --version
uv build --wheel --out-dir /tmp/speechcraft-whisper-wheel packages/model_manager
uv venv --python 3.12 /tmp/speechcraft-whisper-venv
uv pip install --python /tmp/speechcraft-whisper-venv/bin/python /tmp/speechcraft-whisper-wheel/speechcraft_model_manager-0.1.0-py3-none-any.whl
/tmp/speechcraft-whisper-venv/bin/python -c 'import huggingface_hub; print(huggingface_hub.__version__)'
```

The wheel includes the fixed catalog and schema. It installs only the
lightweight model-manager dependencies, not `mlx`, `mlx-whisper`, PyTorch,
faster-whisper, or the dataset worker.

## Fresh install, verification, and reuse

Use dedicated disposable locations. The smoke sets `HF_HOME` and `HF_XET_CACHE`
to isolated cache paths before importing the Hub libraries. Its output reports
the pinned revision, per-file size/hash verification, model-store bytes, Xet
cache bytes, duration, and second-run reuse.

```bash
mkdir -p /tmp/speechcraft-whisper-mac-models /tmp/speechcraft-whisper-mac-xet
/tmp/speechcraft-whisper-venv/bin/python -I packages/model_manager/tests/smoke_whisper.py \
  --download-real-whisper --platform macos-arm64 \
  --model-root /tmp/speechcraft-whisper-mac-models \
  --cache-root /tmp/speechcraft-whisper-mac-xet
```

Run the same command again to confirm verified-install reuse. A network-free
status check is also available:

```bash
/tmp/speechcraft-whisper-venv/bin/python -m speechcraft_models status asr \
  --platform macos-arm64 --root /tmp/speechcraft-whisper-mac-models
```

Expect `integrity_verified: true`, `install_ready: true`, `runtime_ready: null`,
and `reused: true` on the repeat. The status command's result should list
`config.json` and `weights.npz` as verified.

## Interrupted transfer recovery

Run the install command above and press Ctrl+C after the large transfer begins.
The helper leaves its lock-protected `.stage-*` data and Xet cache in place. It
prints a machine-readable result with an exact resume command. Run that command
with the same model/cache directories; do not remove their staging or Xet data
before it completes. After it succeeds, run the fresh command once more and
confirm it reports reuse without a second model download.

If you need to abandon the experiment, remove only these named test paths after
no downloader process is running:

```bash
rm -rf /tmp/speechcraft-whisper-mac-models /tmp/speechcraft-whisper-mac-xet /tmp/speechcraft-whisper-mac-xet-home
```

## Return these results

- Mac model/hardware and `sw_vers` output.
- `python --version`, `uv --version`, and installed `huggingface_hub` version.
- Installed revision (expected `49e6aa286ad60c14352c404340ded53710378a11`).
- JSON result from fresh install and reuse, including file hashes/sizes,
  elapsed time, model-store bytes, and cache bytes.
- Whether interruption/resume completed and whether the next invocation reused
  the verified installation.
- Any filesystem, Xet, or package-install errors.

The selected model is the full `mlx-community/whisper-large-v3-mlx`, not a
quantized model. Passing this smoke proves file acquisition only. MLX runtime
compatibility, transcription behavior, TC behavior, and the Mac CPU fallback
remain separate integration work.
