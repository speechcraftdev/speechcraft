# Apple Silicon bootstrap handoff for Kavin

This checks the shared managed-runtime bootstrap on an actual Apple Silicon
Mac. It does not test MLX, Whisper inference, TC scoring, or production
readiness.

## Prerequisites

- Apple Silicon Mac (M-series), current supported macOS release.
- Node.js 20.9 or later. The eventual app will bundle Node; this is only a
  development-host prerequisite.
- Network access to GitHub release assets and PyPI.
- A SpeechCraft release/build artifact containing this `setup-controller`
  package, the bootstrap manifest, the locked requirements, and the exact
  `speechcraft-model-manager` wheel plus its trusted SHA-256 from the build
  pipeline. Do not create a trust hash by hashing an untrusted downloaded
  wheel. No Homebrew or system Python is used by bootstrap itself.

## Run in an isolated directory

Use a fresh temporary directory on the Mac's APFS disk. Do not point this test
at a real SpeechCraft data directory:

```bash
export SPEECHCRAFT_SETUP_DATA_DIR="$(mktemp -d /tmp/speechcraft-mac-bootstrap.XXXXXX)"
export SPEECHCRAFT_MODEL_MANAGER_WHEEL="/path/from/build-artifacts/speechcraft_model_manager-0.1.0-py3-none-any.whl"
export SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256="<trusted digest supplied by the build pipeline>"
cd setup-controller
npm start -- --mode bootstrap --data-dir "$SPEECHCRAFT_SETUP_DATA_DIR" --port 4317
```

In another terminal, establish a development session and start the job. The
token is read directly from the private file; do not paste it into a URL or
share terminal logs:

```bash
ORIGIN=http://127.0.0.1:4317
curl -sS -c /tmp/speechcraft-mac-cookies.txt \
  -H "Origin: $ORIGIN" \
  -H "Authorization: Bearer $(cat "$SPEECHCRAFT_SETUP_DATA_DIR/bootstrap.token")" \
  -H 'Content-Type: application/json' -d '{}' \
  "$ORIGIN/setup-api/session" > /tmp/speechcraft-mac-session.json
CSRF=$(node -p "JSON.parse(require('fs').readFileSync('/tmp/speechcraft-mac-session.json')).csrf_token")
curl -sS -b /tmp/speechcraft-mac-cookies.txt -H "Origin: $ORIGIN" \
  -H "X-SpeechCraft-CSRF: $CSRF" -H 'Content-Type: application/json' -d '{}' \
  "$ORIGIN/setup-api/start"
```

Observe setup with:

```bash
curl -sS -b /tmp/speechcraft-mac-cookies.txt "$ORIGIN/setup-api/status"
curl -N -b /tmp/speechcraft-mac-cookies.txt -H 'Accept: text/event-stream' \
  "$ORIGIN/setup-api/events"
```

The expected profile is `macos-arm64`. The controller should verify uv 0.12.23,
managed CPython 3.12.15, and the isolated model-manager environment. The final
job is `completed` while `production_ready` remains `false`.

## Reuse and recovery checks

After successful completion, stop the controller with Ctrl-C and restart it
with the same data directory and wheel settings. Re-establish the session (the
bootstrap token rotates), call `POST /setup-api/start` again, and confirm the
verified runtimes are reused. The second job should finish quickly. Inspect the
managed paths under `runtimes/`, `environments/`, `cache/bootstrap/`, and
`staging/bootstrap/`; completed attempt staging should be empty.

For an interruption check, use only this isolated directory: start another
install and send the controller a forced process termination while uv is
active. Restart it against the same data path. Startup must stop any recorded
surviving process group, reconcile the job as interrupted, and require explicit
retry. Retry through the authenticated API and verify bootstrap. Do not use
Ctrl-C for this test because it requests graceful cancellation.

## Return these results

Attach the JSON status snapshot and summarize:

- Mac model/chip and macOS version.
- Node version and `huggingface_hub` is not involved in this stage.
- Actual profile and installed uv/Python versions.
- Job stage/state and `production_ready` value.
- First and reuse run durations; managed data/cache directory sizes.
- Whether any Python/uv downloads were repeated on reuse.
- Forced-interruption recovery and retry result.
- Any macOS permissions, quarantine, executable, filesystem, or dependency
  errors (include sanitized error codes/messages, never the bootstrap token).
- The wheel artifact version/digest identifier supplied by the build pipeline.

This confirms file/runtime bootstrap only. MLX installation, Whisper inference,
GPU behavior, and Transcript Correctness evaluation remain separate work.
