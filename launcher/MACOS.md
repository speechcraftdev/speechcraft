# SpeechCraft on a Mac

SpeechCraft on Apple Silicon uses the native MLX Whisper Large-v3 runtime. It does not use CTranslate2. Windows still uses Ubuntu WSL2.

The package builder must be run on the Mac:

```bash
node launcher/scripts/package-macos.mjs --source-revision "$(git rev-parse HEAD)" --output "$HOME/speechcraft-macos-release"
```

That command was not run on this Linux machine, so no macOS archive was produced. The installer, once an archive exists, is:

```bash
python3 launcher/installer/install-macos.py speechcraft-0.1.0-macos-arm64.tar.gz
```

It installs under `~/Library/Application Support/SpeechCraft` without sudo. The build is not signed or notarized.

FFmpeg is not a redistributed Apple Silicon binary. The controller downloads the pinned FFmpeg source archive `46d899d7a02bc63237472cc4897258d5ddf274ac` (SHA-256 `6626e1f622324597146a8cc90a89bb4c3529f7df55f0a8c998fde55ac5ae4161`, 16,923,502 bytes, recorded from a download on 2026-10-17) and builds it locally with `--disable-gpl` and `--disable-nonfree`. Decode readiness still requires the runtime probe. Product redistribution review remains open.

MLX inference, Community-1, and TitaNet were not executed here. `inference_validated` stays false.

## M4 Pro acceptance

Run `launcher/macos/accept-macos.sh` on the Mac. It is pending until that machine completes it. Do not put a Hugging Face token on the command line.
