# SpeechCraft setup controller

This is an internal Node.js setup controller, independent of the dataset worker,
system Python, ML runtimes, and Next.js. It owns one durable setup job and
exposes a loopback-only authenticated HTTP API. Simulation remains the default
for frontend development. Launcher-selected `bootstrap` mode retains the
original uv/Python/model-manager bootstrap plan. Launcher-selected `runtime`
mode continues through a hash-identified backend dependency environment,
approved model acquisition, runtime validation, and a supervised loopback
FastAPI health check. It reports unavailable worker, FFmpeg, model-authorization,
and inference-probe requirements as blockers and never treats them as ready.
The future public application command remains `speechcraft` and is not created.

## Runtime and storage

Minimum supported runtime is **Node.js 20.9**. The package has no runtime npm
dependencies and uses ES modules plus Node's standard library. Linux tests and
the real bootstrap ran on Node 26.8.2. macOS ARM64 and WSL2 execution still need
host validation. The runtime floor will be bundled by the future launcher.

## Real setup plans

Run the complete controller-owned orchestration plan with:

```bash
npm start -- --data-dir /tmp/speechcraft-setup-dev --mode runtime
```

Trusted runtime inputs are listed in `resources/runtime-manifest.json`. The
backend recipe verifies both `backend/pyproject.toml` and `backend/uv.lock`
before using pinned `uv sync --locked --no-dev --no-install-project` into a
versioned environment. The installed environment is probed with its managed
interpreter and backend imports. It checks for at least 4 GiB plus a 512 MiB
reserve before dependency acquisition. Older verified environment versions
are not removed when a new version is created.

The runtime plan invokes `python -m speechcraft_models install` for Silero and
the platform-selected Whisper Large-v3 artifact. The persistent model root is
`<controller-data>/models`; Hugging Face home, Hub and Xet caches are under
`<controller-data>/cache/bootstrap/huggingface`. The model manager retains
ownership of transfers, resumability, integrity checks and publication. These
files are file-verified only; no inference result is implied.

Worker installation is implemented as a trusted recipe consumer. A profile is
installable only when its manifest entry is marked both `available` and
`validated`, pins the project manifest and lock SHA-256 values, identifies the
managed Python version, environment version, disk estimate, and required
import checks. The checked-in CPU Linux, CUDA Linux, and macOS entries remain
unavailable; the existing CUDA lock has not been validated as a full recipe.
No trusted FFmpeg/FFprobe bundle or inference helper is present. When signed
off helpers are added to `runtime_validation.probes`, the controller checks
the helper digest, runs it through the managed model-manager interpreter with
a bounded timeout and process ownership, and accepts readiness only from a
structured result confirming inference completed. Missing helpers remain
blocked. Pyannote authorization is not integrated and TitaNet's release
blocker remains. These components appear as blocked in status, while an
eligible backend can still be started and checked at `/healthz`.
`production_ready` remains false.

Backend service launch uses a durable ownership gate. The child blocks on
stdin before importing Uvicorn or serving requests; the controller releases
it only after persisting the process ID and identity. A restart that finds a
version-2 `starting` record without a PID can safely clear it because the
child could not have crossed the gate. Older or ambiguous records still fail
closed and require inspection.

The backend runs as a separately recorded process group on `127.0.0.1` with a
dynamically allocated port. It writes project DB/media/exports beneath the
managed application-data subtree and receives worker root/Python paths through
the backend's existing environment interface. The controller owns only the
backend service; the backend continues to own on-demand dataset jobs. Graceful
controller shutdown stops the service. Startup reconciles the previous service
process before exposing the API; ambiguous ownership fails closed.

`POST /setup-api/repair` reruns the selected trusted plan, or retries a failed
or interrupted attempt. It accepts no component path or command. A completed
runtime plan can reuse verified components and repair invalid ones.

See [ARCHITECTURE.md](ARCHITECTURE.md) for state, ownership and readiness
boundaries, and [API.md](API.md) for the expanded plan and component contract.

The default state directory is:

- Linux and WSL2: `$XDG_DATA_HOME/speechcraft/setup-controller`, or
  `~/.local/share/speechcraft/setup-controller` when `XDG_DATA_HOME` is unset.
- macOS: `~/Library/Application Support/SpeechCraft/setup-controller`.

`SPEECHCRAFT_SETUP_DATA_DIR` or `--data-dir` overrides the location. State is
separate from versioned releases, projects, recordings, and models. Tests always
use isolated temporary directories.

The directory is mode `0700`; state and bootstrap files are mode `0600`. State
is one versioned JSON document containing the current job, monotonically
increasing revision, and a bounded 256-event journal. Updates write and fsync a
temporary file, rename atomically, then fsync the directory where supported.
On macOS filesystems that reject directory fsync, atomic rename still prevents
partial JSON, but power-loss durability depends on filesystem guarantees.
There is no silent reset for malformed or unsupported state.

Only one controller process can own a data directory. A private regular lock
file is created with exclusive `open(..., 'wx')` and records PID plus an OS
process-start identity (Linux boot ID and process start ticks; macOS `ps` start
time). This distinguishes normal PID reuse; if identity cannot be established,
ownership fails closed. Stale recovery first takes a second exclusive recovery
marker, then rechecks the original lock file's device/inode and owner nonce
before unlinking it. This prevents an inspect-then-rename race from moving a
new live owner's lock. All contenders honor the marker. If a process dies while
holding that marker, the next start fails with `LOCK_RECOVERY_REQUIRED`; it
does not guess or steal the marker. Stop all controller processes, inspect the
private `.controller.recovery` and `.controller.lock` metadata, confirm the
recorded recovery PID/start identity is no longer live, then remove only the
stale `.controller.recovery` marker. Leave `.controller.lock` in place so the
next start can reclaim it using the inode-checked protocol. Corrupt/unsafe
metadata requires manual inspection and must not be removed based on PID alone.
Local Linux and macOS filesystems are supported; network filesystems are not.

Persistent state schema 1 upgrades atomically to schema 2, which adds a unique
execution-attempt ID. After unexpected termination, persisted `running` or
`cancelling` work is marked `interrupted`; it is never reported as completed or
silently resumed. Cancellation first persists `cancelling`, aborts the current
attempt, and becomes `cancelled` only after that executor settles. Retry is
rejected until the previous attempt is conclusively stopped. Every progress,
failure, and completion callback is scoped to the exact attempt ID. A graceful
SIGINT/SIGTERM waits for owned work to acknowledge cancellation, closes
event streams, and releases ownership. Jobs do not continue while the controller
process is stopped.

Each predefined bootstrap subprocess belongs to one persisted attempt. Before
spawn, the controller durably records a launch intent; after spawn it records
the PID, process-group ID, OS process identity, and a random launch nonce.
Cancellation sends `SIGTERM` to that owned group, then escalates only while the
original group leader identity is still present. The record is cleared only
after the entire recorded process group is confirmed absent. If the leader has
exited while group members remain, identity is ambiguous, or persistence of
the result fails, ownership remains recorded as `termination_uncertain` and
retry is blocked. Startup performs this reconciliation regardless of whether
the new controller is configured for simulation or bootstrap. A retry must
also use the executor mode recorded on that job.

This ownership boundary covers the process group created for the controller's
predefined `uv` operations. POSIX does not provide a portable way for an
unprivileged process to prove that a descendant deliberately detached into a
new session has stopped. These trusted commands are expected not to daemonize;
if an owned group remains after the leader exits, SpeechCraft fails closed and
requires manual inspection. A future bootstrap command that can daemonize
must add OS-level containment before it is run as an owned installation task.
On macOS, identity checks additionally require the per-launch nonce to remain
visible in `ps`; if this cannot be established, recovery fails closed. Linux
was exercised locally; macOS and WSL2 recovery behavior still requires host
validation. Older Linux ownership records remain verifiable using boot ID and
start ticks plus a fresh process-group check. Older macOS records without a
launch nonce cannot be safely signaled and require manual inspection rather
than automatic recovery.

## Start locally

From this directory:

```bash
npm start -- --data-dir /tmp/speechcraft-setup-dev --port 4317
```

The controller binds only to `127.0.0.1`. Startup prints its address and the
private bootstrap-token file path, never the token. Keep the state directory
private. Simulation is the default. Real runtime bootstrap is selected by the
launcher with `--mode bootstrap` and requires a locally shipped model-manager
wheel plus trusted SHA-256 configuration. A faster simulation uses
`--step-delay-ms 0`.

In another terminal, establish an authenticated browser/API session. This
example uses only Node and curl; the bearer token is read from the mode-0600
file and is never placed in a URL:

```bash
DATA_DIR=/tmp/speechcraft-setup-dev
TOKEN_FILE="$DATA_DIR/bootstrap.token"
ORIGIN=http://127.0.0.1:4317
curl -sS -D /tmp/speechcraft-setup-session.headers \
  -c /tmp/speechcraft-setup-cookies.txt \
  -H "Origin: $ORIGIN" \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  -H 'Content-Type: application/json' \
  -d '{}' "$ORIGIN/setup-api/session" > /tmp/speechcraft-setup-session.json
```

Read the CSRF token without Python:

```bash
CSRF=$(node -p "JSON.parse(require('fs').readFileSync('/tmp/speechcraft-setup-session.json')).csrf_token")
```

Start, inspect, stream events, cancel, or retry (retry is available only after a
failed/cancelled/interrupted job):

```bash
curl -sS -b /tmp/speechcraft-setup-cookies.txt -H "Origin: $ORIGIN" \
  -H "X-SpeechCraft-CSRF: $CSRF" -H 'Content-Type: application/json' \
  -d '{}' "$ORIGIN/setup-api/start"
curl -sS -b /tmp/speechcraft-setup-cookies.txt "$ORIGIN/setup-api/status"
curl -N -b /tmp/speechcraft-setup-cookies.txt \
  -H 'Accept: text/event-stream' "$ORIGIN/setup-api/events"
curl -sS -X POST -b /tmp/speechcraft-setup-cookies.txt -H "Origin: $ORIGIN" \
  -H "X-SpeechCraft-CSRF: $CSRF" -H 'Content-Type: application/json' \
  -d '{}' "$ORIGIN/setup-api/cancel"
```

After a controller restart, its bootstrap token is rotated. Re-run session
establishment. Browser refresh within the same process uses
`GET /setup-api/session` with the HTTP-only cookie to refresh the CSRF token.
See [API.md](API.md) for the full stable request, response, and SSE contract.

## Security boundary

Loopback binding is combined with a random startup bootstrap secret, random
HTTP-only `SameSite=Strict` session cookies, per-session CSRF tokens, Host and
Origin allowlists, a 16 KiB JSON limit, fixed routes/methods, and no CORS. Only
the narrow health endpoint is unauthenticated. Mutating endpoints accept no
command, executable, argument, URL, path, or script fields. There is no shell
execution in this package.

Local HTTP uses cookies without `Secure`. A trusted HTTPS reverse proxy must
configure exact `SPEECHCRAFT_SETUP_ALLOWED_HOSTS` and
`SPEECHCRAFT_SETUP_ALLOWED_ORIGINS`, set
`SPEECHCRAFT_SETUP_SECURE_COOKIES=1`, and proxy `/setup-api/*` through the same
browser origin as Next.js. Proxy integration is not implemented in this
milestone.

### Launcher-to-browser session handoff

The launcher must keep the bootstrap capability out of browser URLs, page
JavaScript, console output, and arbitrary websites. The planned handoff is a
same-origin proxy operation: the launcher holds the private bootstrap token in
memory (or reads the mode-0600 file), and the frontend makes a credential-free
`POST /setup-api/session` to the application origin. The launcher-side proxy
uses the token only on its private loopback request to the controller, then
forwards the controller's HTTP-only session `Set-Cookie` and CSRF response to
that same-origin browser request. The token is never forwarded to the browser.
The controller continues validating exact Host and Origin values; the proxy
must preserve the browser Origin and must not allow cross-origin CORS. The
cookie remains `HttpOnly` and `SameSite=Strict`, and mutations retain CSRF
checks. The proxy can be HTTPS with `Secure` cookies or local HTTP with the
loopback cookie policy. For SSH access, keep the controller and proxy bound to
loopback on the remote host and forward the application port; the browser uses
the forwarded localhost origin, and the proxy's explicit Host/Origin allowlist
must match it. Launcher integration must still validate first-run behavior,
proxy header handling, and cookie behavior through SSH tunnels.

## Managed runtime bootstrap

The pinned manifest is [resources/bootstrap-manifest.json](resources/bootstrap-manifest.json).
It supports Linux x86-64 (including Ubuntu WSL2 on WSL's native filesystem)
and macOS ARM64. It pins **uv 0.12.23** and **CPython 3.12.15**. uv release
archives are checked against exact size/SHA-256 values, then the extracted uv
executable is independently checked against its own digest and version. The
Python build and SHA-256 values come from uv's pinned embedded
python-build-standalone metadata; `uv python install` verifies the actual
archive against that metadata. This establishes content integrity, not
byte-for-byte reproducible runtime construction.

Managed files live below the persistent data directory:

```text
runtimes/uv/0.12.23/<profile>/uv
runtimes/cpython-3.12.15+20261003/<profile>/...
environments/speechcraft-model-manager/<version-python-platform-wheel-lock-id>/
cache/bootstrap/uv/
cache/bootstrap/huggingface/{hub,xet}/
cache/bootstrap/{home,tmp}/
staging/bootstrap/attempt-<attempt UUID>/
quarantine/bootstrap/
```

The app release owns the bootstrap manifest and hash-locked dependency file.
The launcher supplies an exact locally shipped `speechcraft-model-manager`
wheel and its trusted SHA-256; no latest PyPI package is used. The wheel and
dependency-lock digests are part of the environment's versioned directory, so
a new wheel or lock produces a separate target and leaves older environments
untouched. A released launcher must embed the wheel identity in trusted
configuration. The environment contains no inference packages.

Bootstrap invokes uv by absolute path, installs its pinned managed Python,
validates version/platform/path/stdlib imports, then creates a dedicated
environment and installs hash-pinned binary dependencies and the trusted wheel.
It does not use system Python, pip, Conda, Homebrew, or user site-packages.
Caller `UV_*`, `PIP_*`, Python, Conda, and Hugging Face credential variables
are not inherited. Executable lookup, certificate and proxy variables are
allowed; cache, home and temporary paths are redirected into managed storage.

Capacity checks precede transfer/install stages. Estimates include the uv
archive and executable, Python archive/environment, manager environment/cache,
and a 512 MiB margin. uv can temporarily retain transfer/cache data alongside
the installed runtime, so peak use can exceed the estimate. A successful job
removes its attempt staging directory. Interrupted files remain available for
diagnosis until retry, when only UUID-owned staging directories are removed
under controller ownership. Corrupt versioned components are moved to the
private quarantine directory and retained for diagnosis; cleanup is manual and
must be limited to that directory after stopping SpeechCraft.

WSL is detected from actual kernel identity, not the chosen artifact label.
The data root and runtime, environment, cache, staging, and temporary paths are
checked against `/proc/self/mountinfo`; Windows-backed mounts are rejected even
when reached outside `/mnt`. If WSL filesystem ownership is ambiguous, setup
fails closed. The bootstrap never touches model weights, projects, recordings,
or exports.

### Opt-in real-network smoke test

Build the internal wheel as a development preparation step (this is not part of
the end-user bootstrap), then record its digest from the trusted build output:

```bash
cd packages/model_manager
uv build --wheel --out-dir /tmp/speechcraft-model-manager-wheel
sha256sum /tmp/speechcraft-model-manager-wheel/speechcraft_model_manager-0.1.0-py3-none-any.whl
cd ../../setup-controller
SPEECHCRAFT_MODEL_MANAGER_WHEEL=/tmp/speechcraft-model-manager-wheel/speechcraft_model_manager-0.1.0-py3-none-any.whl \
SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256=<trusted-wheel-sha256> \
node scripts/real-bootstrap-smoke.js
```

The bootstrap script creates an isolated temporary data directory, performs
bootstrap, restarts the controller, confirms reuse, and retains its data
directory. It downloads no model weights. The runtime smoke uses the same
isolated-store pattern but downloads platform-selected Whisper Large-v3 and
Silero and may require several gigabytes:

```bash
SPEECHCRAFT_MODEL_MANAGER_WHEEL=/absolute/trusted/path/to/wheel.whl \
SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256=<trusted-wheel-sha256> \
SPEECHCRAFT_APPLICATION_ROOT=/absolute/path/to/speechcraft-release \
node scripts/real-runtime-smoke.js
```

Set `SPEECHCRAFT_RUNTIME_SMOKE_DIR` to a fresh directory on the intended
filesystem when `/tmp` is unsuitable. It retains the isolated directory for
recovery inspection. The release launcher must supply the exact wheel identity
rather than computing and trusting one at install time. See the macOS and WSL
handoff notes for host checks.

## Tests

Run from this directory, with Node 20.9 or later:

```bash
npm test
# or
node --test
```

Tests use the filesystem state store, HTTP server, temporary directories, local
artifact transfers, actual child processes and SSE connections. They cover
controller ownership/recovery and security, retries/cancellation, WSL policy
fixtures, safe archive extraction, backend service lifecycle and bootstrap with
offline stand-in tools. Default tests make no network requests or managed
Python/model installations. Real network smoke tests are opt-in.

## Next PR

The Linux x86-64 CPU worker recipe is now pinned, controller-installable and
validated on this host for environment imports, Silero ONNX, offline Whisper
Large-v3, and experimental local TitaNet inference. CUDA remains unvalidated
because this host has no working NVIDIA driver. Managed FFmpeg/FFprobe and
trusted inference-probe recipes are still unavailable. Full dataset processing
therefore remains blocked until a trusted FFmpeg package and authorized local
Community-1 snapshot are available. TitaNet's licensing/provenance and score
calibration remain separate release gates. The Mac worker runtime still needs
Kavin's M4 Pro validation.
