# SpeechCraft on a Mac

SpeechCraft on Apple Silicon uses the native MLX Whisper Large-v3 runtime. It does not use CTranslate2. Windows still uses Ubuntu WSL2.

The package command exports one Git revision, builds the standalone frontend, downloads the pinned Darwin ARM64 Node runtime, and writes `speechcraft-<version>-macos-arm64.tar.gz` plus checksum and release files. On a non-Mac host it verifies the Node binary by its Mach-O header and does not execute it.

```bash
node launcher/scripts/package-macos.mjs --source-revision "$(git rev-parse HEAD)" --output "$HOME/speechcraft-macos-release"
```

A cross-build from Linux of revision `2c6fa4809f7816dec6a6e6f1a8704c3dcc65b7a3` produced `/tmp/speechcraft-macos-2c6fa48/speechcraft-0.1.0-macos-arm64.tar.gz` (56,210,494 bytes, SHA-256 `30762f283eb35717d924c556b6feb20998e00276e1f1731edbd4d22d3197f8c3`). That archive is not committed. The bundled `runtime/node` is a Mach-O arm64 executable and was not run.

The installer is still a Python 3 program. It does not yet have a compiled Darwin bootstrap, so a machine without Python cannot run it. With Python available:

```bash
python3 launcher/installer/install-macos.py speechcraft-0.1.0-macos-arm64.tar.gz
```

It installs under `~/Library/Application Support/SpeechCraft` without sudo and appends a marked PATH line to `~/.zshrc`. A temporary-home install of the archive above registered `.../SpeechCraft/bin/speechcraft`. That wrapper was not executed, because it launches the Darwin Node binary. The build is not signed or notarized.

FFmpeg is not a redistributed Apple Silicon binary. The controller downloads the pinned FFmpeg source archive `46d899d7a02bc63237472cc4897258d5ddf274ac` (SHA-256 `6626e1f622324597146a8cc90a89bb4c3529f7df55f0a8c998fde55ac5ae4161`, 16,923,502 bytes, recorded from a download on 2026-10-17) and builds it locally with `--disable-gpl` and `--disable-nonfree`. Decode readiness still requires the runtime probe. Product redistribution review remains open.

MLX inference, Community-1, and WeSpeaker ECAPA were not executed here. `inference_validated` stays false.

`speechcraft uninstall` is implemented for the Linux per-user install only. This Mac package does not register that command, and uninstall has not been verified on macOS.

## M4 Pro acceptance

Run `launcher/macos/accept-macos.sh` on the Mac. It is pending until that machine completes it. Do not put a Hugging Face token on the command line.
