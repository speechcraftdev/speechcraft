# SpeechCraft setup controller foundation

This is an internal Node.js setup controller, independent of the dataset worker,
Python, ML runtimes, model caches, and Next.js. It owns one durable setup job
and exposes a loopback-only authenticated HTTP API. All operations in this
milestone are simulated; no Python environment, dependency, model, or backend is
installed. The future public application command remains `speechcraft` and is
not created here.

## Runtime and storage

Minimum supported runtime is **Node.js 20.9**. The package has no runtime npm
dependencies and uses ES modules plus Node's standard library. This version
aligns with the current Next.js runtime floor and works with a bundled Node
runtime later.

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

Only one controller process can own a data directory. Ownership uses atomic
directory creation and records PID plus an OS process-start identity (Linux
boot ID and process start ticks; macOS `ps` start time), so a reused PID is not
mistaken for the original owner. Stale ownership is reclaimed only after the
prior process is confirmed dead or its process-start identity differs. An
ambiguous/corrupt lock is left in place and requires inspection; it is never
removed merely because a PID check failed. Locks and atomic rename provide local
filesystem guarantees; network filesystems are unsupported.

After unexpected termination, persisted `running` work is marked
`interrupted`; it is never reported as completed or silently resumed. A graceful
SIGINT/SIGTERM persists cancellation, aborts simulated work, closes event streams,
and releases ownership. Jobs do not continue while the controller process is
stopped.

## Start locally

From this directory:

```bash
npm start -- --data-dir /tmp/speechcraft-setup-dev --port 4317
```

The controller binds only to `127.0.0.1`. Startup prints its address and the
private bootstrap-token file path, never the token. Keep the state directory
private. A faster deterministic simulation can use `--step-delay-ms 0`.

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

## Tests

Run from this directory, with Node 20.9 or later:

```bash
npm test
# or
node --test
```

Tests use the real filesystem state store, HTTP server, temporary directories,
local fetches, SSE connections, and actual child controller processes. They
cover concurrent starts and controller ownership, browser disconnect/reconnect,
multiple clients, retry, cancellation, crash recovery, corrupt state,
CSRF/Origin/Host checks, unsafe fields, and simulated completion. They do not
install Python or contact external services.

## Next PR

Add the launcher handoff and same-origin Next.js proxy contract: the launcher
should start/stop this package, provide the bootstrap capability without
printing it, and configure the persistent data directory and exact proxy
Host/Origin values. Then Kavin's Preparing UI can consume `/setup-api/status`
and `/setup-api/events`. Keep real Python/model tasks in a later milestone with
explicit task-specific cancellation, retry, and recovery semantics; this
controller currently proves only the state and HTTP foundation.
