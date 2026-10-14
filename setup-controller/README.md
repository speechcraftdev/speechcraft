# SpeechCraft setup controller

This is an internal Node.js setup controller, independent of the dataset worker,
system Python, ML runtimes, and Next.js. It owns one durable setup job and
exposes a loopback-only authenticated HTTP API. Simulation remains the default
for frontend development. Launcher-selected `bootstrap` mode installs pinned
uv, managed CPython, and an isolated lightweight `speechcraft_models`
environment. It does not install models or make SpeechCraft production-ready.
The future public application command remains `speechcraft` and is not created.

## Runtime and storage

Minimum supported runtime is **Node.js 20.9**. The package has no runtime npm
dependencies and uses ES modules plus Node's standard library. Linux tests and
the real bootstrap ran on Node 26.8.2. macOS ARM64 and WSL2 execution still need
host validation. The runtime floor will be bundled by the future launcher.

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

Each predefined bootstrap subprocess belongs to one persisted attempt.
Cancellation terminates its process group, escalates after a bounded grace
period, waits for exit/reap, and only then persists terminal `cancelled` or
permits retry. On restart, the controller verifies process identity and stops
surviving owned groups before revalidating artifacts.

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

The script creates an isolated temporary data directory, performs bootstrap,
restarts the controller, confirms reuse, and retains its data directory for
inspection. It downloads no model weights. The release launcher must provide
the exact wheel identity rather than computing and trusting one at install
time. See [MACOS_BOOTSTRAP_HANDOFF.md](MACOS_BOOTSTRAP_HANDOFF.md) for Kavin's
Apple Silicon handoff.

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
fixtures, safe archive extraction, and the complete five-stage bootstrap with
offline stand-in tools. Default tests make no network requests or Python
installations. The real network smoke test is opt-in.

## Next PR

Integrate the controller with the launcher and same-origin Next.js proxy,
provide the trusted manager-wheel digest, and let Kavin's Preparing UI consume
`/setup-api/status` and `/setup-api/events`. Subsequent work may provision ML
environments and models with explicit artifact, cancellation, retry, and
recovery contracts. This bootstrap installs no model weights or inference
runtime and cannot report workstation production readiness.
