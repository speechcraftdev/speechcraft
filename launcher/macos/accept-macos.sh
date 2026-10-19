#!/bin/sh
# Pending native Apple Silicon acceptance. This script does not claim success
# when it is not run on an M4 Pro or another Apple Silicon Mac.
echo "PENDING_NATIVE_MACOS: run this on the Mac after building the archive."
if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "Refusing to pretend this ran on Apple Silicon."
  exit 2
fi
echo "1. Confirm uname -m is arm64."
echo "2. node launcher/scripts/package-macos.mjs --source-revision \"\$(git rev-parse HEAD)\" --output \"\$HOME/speechcraft-macos-release\""
echo "3. python3 launcher/installer/install-macos.py \"\$HOME/speechcraft-macos-release/speechcraft-0.1.0-macos-arm64.tar.gz\""
echo "4. Open a new terminal and run speechcraft. Confirm /setup in the default browser."
echo "5. Finish setup in the browser. Enter a Hugging Face token only in the masked field."
echo "6. Confirm managed Python, the macOS worker, a local FFmpeg build, Silero, and MLX Whisper Large-v3."
echo "7. Run real MLX transcription and Transcript Correctness."
echo "8. Run authorized offline Community-1 diarization and WeSpeaker ECAPA Speaker Purity inference."
echo "9. Process a multi-speaker recording, use Clip Lab, and export the canonical dataset."
echo "10. speechcraft stop, then speechcraft. Confirm /login, no second model download, and no new token."
echo "11. speechcraft status and speechcraft stop leave no owned services behind."
exit 2
