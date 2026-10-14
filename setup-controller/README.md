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
SIGINT/SIGTERM waits for simulated work to acknowledge cancellation, closes
event streams, and releases ownership. Jobs do not continue while the controller
process is stopped.

When predefined installation subprocesses are added, they must be owned by the
attempt that launched them. Cancellation must terminate the owned process
group, escalate only after a bounded grace period, wait for child exit/reap,
and only then persist terminal `cancelled` or permit retry. A controller must
not release installation ownership while a child can still write managed files.

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
