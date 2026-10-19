# SpeechCraft on Apple Silicon macOS

## Current Linux-side status

**Full native Apple Silicon acceptance: NOT YET VALIDATED.** The candidate workflow now adds an Apple Silicon install-and-launch smoke job that downloads the combined artifacts from the same workflow run, regenerates the shared installer for an isolated loopback source, installs the actual archive into a disposable home, launches the setup controller and frontend, checks status/restart/stop, and verifies data-preserving uninstall. It does not install ML models or validate diarization, transcription, FFmpeg compilation, or exports. A passing job is limited startup evidence, not full Mac acceptance.

Latest candidate: source commit `87e640e471aab6801a5f9b70fef5f5b24bc2b1c2`, run [39306281544](https://github.com/speechcraftdev/speechcraft/actions/runs/39306281544), artifact `speechcraft-private-candidate-87e640e471aab6801a5f9b70fef5f5b24bc2b1c2`. It contains `speechcraft-0.1.1-macos-arm64.tar.gz` (SHA-256 `8bbe50a15f89877ceb910c02c7075903a7a06104d4c7abe5062ac03a343e65bc`) and `speechcraft-0.1.1-linux-x86_64.tar.gz` (SHA-256 `d338c7b204ca96bf7e1f2d0a91a702f35105fcb6d067c2385747c13443f44ace`), with matching release manifests and checksums, the platform installers, and shared `install.sh`. This run passed the Apple Silicon install/launch/status/restart/stop/uninstall smoke, but did not run ML or audio processing. Download it with `gh run download 39306281544 --repo speechcraftdev/speechcraft --name speechcraft-private-candidate-87e640e471aab6801a5f9b70fef5f5b24bc2b1c2`. No static HTTPS installer URL has been configured or verified; the smoke tested the generated bootstrap against same-run candidate assets over explicit loopback HTTP.

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

Known unverified platform risks include Gatekeeper behavior for an unsigned archive, managed Mac Python wheels, MLX inference, CPU Pyannote and ECAPA inference, native FFmpeg compilation, browser opening, long-run process recovery, upgrade rollback on the real filesystem, and complete audio export. Read the exact workflow result before using a candidate; do not treat the earlier packaging-only run or any Linux simulation as a native install result.
