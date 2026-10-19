# SpeechCraft on Apple Silicon macOS

## Current Linux-side status

**Native Apple Silicon execution: NOT YET VALIDATED.** No macOS archive has been built or run in this Linux workspace. The new `package-release-candidate.yml` workflow builds the Linux package on Ubuntu and the Mac package on a native Apple Silicon `macos-15` runner, then combines the exact-revision assets and generates one platform-detecting `install.sh`. It uploads a private, expiring workflow artifact; it does not create a GitHub Release or version tag.

Start the workflow on the exact feature-branch commit to be tested. Download `speechcraft-private-candidate-<commit>` and retain the workflow URL, full source commit, archive filename, and SHA-256 values. Those values are intentionally not fabricated in this source handoff. The generated bootstrap is bound to both platform archives and the same application version and source revision.

To prepare a private HTTPS directory, copy every file from the workflow artifact without renaming it, preserve byte-for-byte contents, and serve directory listings only if your host requires them. Generate the bootstrap for that exact directory URL using:

```bash
node launcher/scripts/generate-bootstrap.mjs \
  --tag v0.1.1-internal.1 \
  --source-revision <40-character-tested-commit> \
  --asset-base-url https://<your-private-host>/<candidate-directory> \
  --output <downloaded-candidate-directory>
```

The output directory must contain both platform archives, their `.sha256` and `.release.json` files, `install-linux.py`, `install-linux.sh`, and `install-macos.sh`. Upload the newly generated `install.sh` alongside them. Do not publish the private candidate publicly. A real URL exists only after a private HTTPS host has been configured and every downloaded file has been checked there.

## Candidate installation

The target must be Apple Silicon (`uname -m` reports `arm64`), running macOS 14.0 or newer. Although pinned Node.js 24 supports an older macOS baseline ([Node.js platform support](https://github.com/nodejs/node/blob/v24.x/BUILDING.md#supported-platforms)), locked runtime dependencies include Apple Silicon wheels tagged for macOS 14.0, including MLX and ONNX Runtime. The archive and command wrapper are unsigned and unnotarized. Gatekeeper may prevent execution; do not disable Gatekeeper globally. Proper Developer ID signing and notarization are required before broad distribution.

After the private HTTPS directory has been verified, the intended user workflow is:

```bash
curl -fsSL https://<verified-private-installer-url>/install.sh | sh
# Open a new terminal, or run: source ~/.zprofile
speechcraft
speechcraft status
speechcraft stop
speechcraft uninstall --yes
```

The installer uses only macOS-provided shell tools and does not need system Python, Homebrew, a preinstalled Node runtime, or sudo. It installs the app under `~/Library/Application Support/SpeechCraft/app`, stores the command under `.../bin/speechcraft`, and preserves `setup-controller` managed data during upgrades and normal uninstall. `uninstall --purge` is explicit and limited to the default SpeechCraft managed-data directory. The installer cannot alter the parent shell's `PATH` when it is piped to `sh`; it updates `~/.zprofile` idempotently and explains how to refresh the current shell.

Reserve at least 40 GB of free disk space before setup, plus room for source recordings, exports, and caches. The locked Mac worker profile estimates about 12 GiB; the backend environment estimates 4 GiB. Model preparation downloads roughly 3.1 GB for MLX Whisper Large-v3, plus Silero, ECAPA, and gated Community-1 weights and temporary build/cache files. FFmpeg is built locally from the pinned source, not redistributed as a binary. Its preparation requires Apple's Command Line Tools (`xcode-select --install`); the setup controller reports this dependency and does not install Homebrew or unrelated tools. FFmpeg redistribution review remains open.

## Native Mac acceptance handoff

Run `launcher/macos/accept-macos.sh <candidate-directory>` on the Apple Silicon Mac. Its environment checks do not count as acceptance; it intentionally exits with `NOT YET VALIDATED` until an agent performs and records the real tests below. Collect the exact candidate source revision, all three archive hash files, private installer URL if configured, `sw_vers`, `uname -a`, `file`/`lipo` output for `runtime/node` and frontend `.node` modules, setup-controller logs, backend logs, model/runtime selections, and test results. Do not include secrets or Hugging Face tokens in logs.

1. Verify the archive checksum and release manifest before installation. Confirm the package is ARM64 Mach-O and contains no Linux ELF executable modules.
2. Install into a clean test account using the shared HTTPS installer. Verify new-terminal and sourced `~/.zprofile` command lookup; run `speechcraft`, `status`, and `stop`.
3. Complete managed runtime setup. Confirm pinned uv and CPython, locked backend and `macos-arm64` worker environments, MLX Whisper selection, CPU Pyannote and ECAPA selections, and no CUDA device or Linux library path. Do not mark acceleration ready without a successful inference probe.
4. Confirm FFmpeg preparation identifies missing Command Line Tools clearly. With tools available, build the pinned LGPL configuration and verify both managed `ffmpeg` and `ffprobe` paths and the decode probe.
5. Exercise WAV, MP3, FLAC, M4A/AAC, OGG/Opus, and AIFF uploads. Verify chunked localhost upload progress, integrity checks, conversion, project persistence, and source preservation.
6. Run actual MLX Large-v3 transcription, authorized offline Community-1 diarization, ECAPA inference, Clip Lab, and complete export. Verify all backend services bind only to `127.0.0.1`.
7. Test launch, browser restart behavior, `status`, `stop`, restart recovery, interrupted setup recovery, update to a newer candidate, repeated same-version installation, rollback, and uninstall. Confirm normal uninstall retains projects, models, downloads, logs, and exports; test `--purge` only against disposable test data.
8. Record diagnostics and actual outcomes. Native acceptance is complete only after the full UI pipeline, update, recovery, and data-preservation checks pass on the Mac.

Known unverified platform risks include Darwin standalone frontend dependency compatibility, Gatekeeper behavior for an unsigned archive, the managed Mac Python wheels, MLX inference, CPU Pyannote and ECAPA inference, native FFmpeg compilation, browser opening, process recovery, archive install rollback, and complete audio export. This Linux-side task makes no claim that any of those Mac executions passed.
