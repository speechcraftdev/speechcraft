# WSL2 runtime storage handoff

Run the controller from Ubuntu WSL2 with Node >=20.9. Keep the SpeechCraft data
directory on the WSL Linux filesystem (for example `$HOME/.local/share/speechcraft`),
not under `/mnt/c`, `/mnt/d`, a Windows-backed bind mount, or a symlink to one.

```bash
mkdir -p "$HOME/.local/share/speechcraft-stage4c"
findmnt -T "$HOME/.local/share/speechcraft-stage4c" -o TARGET,FSTYPE,SOURCE
cd setup-controller
SPEECHCRAFT_SETUP_DATA_DIR="$HOME/.local/share/speechcraft-stage4c" \
  npm start -- --mode runtime --data-dir "$HOME/.local/share/speechcraft-stage4c"
```

The controller checks actual WSL identity and mount backing for managed state,
runtimes, caches and temporary storage. Linux model artifacts are selected; the
`windows-wsl2` model label does not influence filesystem policy. HF home, Hub,
Xet, uv and temporary caches are set below the managed WSL data directory.

For validation, authenticate through the development session described in
`API.md`, start the runtime plan, and inspect status/diagnostics. Confirm the
profile is `linux-x86_64`, WSL is true, the model manager selects Linux
CTranslate2 Whisper, and every managed path is WSL-native. Repeat with a
Windows-backed model root/cache and a symlink or bind mount to verify it fails
closed. Return Ubuntu/WSL kernel and Node versions, `findmnt` output for each
tested root, and sanitized status/error output. Simulated unit tests do not
count as real WSL validation.
