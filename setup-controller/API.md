# Setup Controller API v4

All routes use `/setup-api`. JSON responses are UTF-8 and `Cache-Control: no-store`.
The controller binds only to `127.0.0.1`. The eventual application must proxy
`/setup-api/*` through the same browser origin as Next.js; do not enable wildcard
CORS or expose the controller port to a network interface.

## Authentication and browser session

At startup the launcher-compatible controller creates a random 256-bit bootstrap
secret in `bootstrap.token` inside the private controller data directory
(permissions `0600`, directory `0700`). The controller never prints the secret.
The local launcher reads this file and establishes a session:

```http
POST /setup-api/session
Host: 127.0.0.1:4317
Origin: http://127.0.0.1:4317
Authorization: Bearer <startup-secret>
Content-Type: application/json

{}
```

Response `201` sets a random session cookie with `HttpOnly`, `SameSite=Strict`,
`Path=/setup-api`, and an eight-hour expiry. `Secure` is enabled when the
controller is configured behind a trusted HTTPS proxy. The JSON body contains a
CSRF token for the authenticated session. After a browser refresh, call
`GET /setup-api/session` with the cookie to obtain the CSRF token again. The
bootstrap secret is rotated each controller start, so a launcher establishes a
new browser session after restart.

The production launcher must not put this secret in a browser URL, JavaScript,
or a console log. The planned browser handoff is for the launcher-owned
same-origin proxy to receive a credential-free `POST /setup-api/session`, make
the bearer-authenticated session request privately over loopback, and forward
only the HTTP-only session cookie and CSRF response to the browser. Host and
Origin remain validated end-to-end; no wildcard CORS is added. For HTTPS proxy
access use `Secure` cookies. For SSH-tunneled localhost access, keep the
controller/proxy bound to remote loopback and configure the forwarded Host and
Origin explicitly. This proxy handoff still requires launcher integration
testing.

Every route except `/health` requires the session cookie. Mutations also require
an allowed `Origin` and `X-SpeechCraft-CSRF`. Host and Origin are allowlisted;
there is no CORS response. Credentials are never accepted in query parameters.
For a reverse proxy, configure `SPEECHCRAFT_SETUP_ALLOWED_HOSTS`,
`SPEECHCRAFT_SETUP_ALLOWED_ORIGINS`, and
`SPEECHCRAFT_SETUP_SECURE_COOKIES=1` explicitly. The proxy must preserve the
browser Origin and use an allowed Host value.

Session response:

```json
{
  "csrf_token": "<random-session-csrf-token>",
  "expires_at": "2026-10-14T18:00:00.000Z"
}
```

The cookie is separate in the `Set-Cookie` header and is never readable by page
JavaScript.

## Job states and stages

States are `idle`, `running`, `waiting_for_user`, `cancelling`, `failed`, `cancelled`,
`interrupted`, and `completed`. `idle` is the snapshot state when no job exists;
the other values belong to a persisted job. Legal transitions are enforced by
the controller. Retry is an explicit new attempt on the existing job ID. State
schema 1 migrates atomically to schema 2.

State payload schema remains version 2. Simulation mode uses these ordered
stages:

1. `inspect_system`
2. `bootstrap_runtime`
3. `install_dependencies`
4. `install_models`
5. `validate_runtime`

The launcher may select predefined real modes at controller startup. The
browser cannot select a mode or change the plan. `bootstrap` mode retains the
small bootstrap plan below. `runtime` mode uses this ordered plan:

1. `inspect_system`
2. `install_uv`
3. `install_python`
4. `install_model_manager`
5. `validate_bootstrap`
6. `install_backend_environment`
7. `install_worker_environment`
8. `install_models`
9. `validate_runtime`
10. `start_services`
11. `verify_services`

Runtime mode installs approved Silero and platform-selected Whisper files
through `speechcraft_models`, installs backend dependencies from the trusted
lock and starts the backend only after loopback health succeeds. Worker
profiles, FFmpeg/FFprobe artifacts, the pyannote authorization consumer,
TitaNet release approval, and inference helpers are unavailable and stay
blocked. `completed` means the selected controller plan finished, not that
every component is ready.

The earlier real `bootstrap` mode uses these ordered stages:

1. `inspect_system`
2. `install_uv`
3. `install_python`
4. `install_model_manager`
5. `validate_bootstrap`

`completed` means the selected plan finished. It does not itself establish
inference or workstation readiness. `production_ready` is always `false` in
API v4. `bootstrap_ready` covers only uv, managed CPython and model-manager
verification; `files_verified` does not mean `runtime_ready`.

## Snapshot and job shape

`GET /setup-api/status` returns the current authoritative snapshot. It requires
the session cookie. Example while running:

```json
{
  "controller_status": "running",
  "job": {
    "schema_version": 2,
    "id": "a676d84c-341a-4eae-9bd7-8b71dd149647",
    "state": "running",
    "stage": "install_models",
    "task_id": "simulated:install_models",
    "created_at": "2026-10-14T10:30:00.000Z",
    "updated_at": "2026-10-14T10:30:02.000Z",
    "started_at": "2026-10-14T10:30:00.000Z",
    "completed_at": null,
    "attempt_count": 1,
    "attempt_id": "953f50e6-ec29-4fe9-a5b6-0d0b15d8ed8c",
    "revision": 18,
    "progress": {
      "phase": "installing",
      "current": null,
      "total": null,
      "percent": null,
      "message": "Installing the pinned managed Python runtime."
    },
    "mode": "bootstrap",
    "bootstrap_status": {
      "profile": "linux-x86_64",
      "wsl": false,
      "uv_verified": true,
      "python_verified": true,
      "model_manager_verified": false,
      "bootstrap_ready": false,
      "production_ready": false
    },
    "error": null
  },
  "state_revision": 18,
  "mode": "bootstrap",
  "simulated": false,
  "setup_plan": "bootstrap-v1",
  "bootstrap_ready": false,
  "components": null,
  "production_ready": false
}
```

When there is no job, `job.state` is `idle`, its ID/stage/task/timestamps and
progress/error are `null`, and `attempt_count` is zero. Revisions increase on
every persisted state or progress update.

## Operations

All POST operations require `Content-Type: application/json`, a JSON object (up
to 16 KiB), the session cookie, allowed Origin, and the CSRF header. The current
API accepts only `{}`; commands, paths, URLs, and arbitrary options are rejected.

### `POST /setup-api/start`

Starts the configured plan. Returns `202` with the new job, or `200` with the
existing job when it is already running/waiting. A completed job may be started
again as a new job ID; bootstrap rechecks and reuses verified components. Other
failed, cancelled, or interrupted jobs must use explicit retry on the existing
job ID. Concurrent starts still produce one active job.

### `POST /setup-api/retry`

Retries the same job ID from `failed`, `cancelled`, or `interrupted`. The attempt
count increases. Returns `202`. Other states return `409 RETRY_NOT_ALLOWED`.

### `POST /setup-api/cancel`

Persists `cancelling` and aborts owned work. The executor must settle before
the job becomes terminal `cancelled`; retry is rejected while it is still
stopping. Returns `202` when cancellation begins and `200` for a repeated
request. An already failed or completed job cannot be cancelled. `attempt_id`
changes on every retry, and events from older attempt IDs cannot change the
current job.

### `POST /setup-api/repair`

Reruns the launcher-selected plan or safely retries a failed/interrupted job.
Active plans are returned idempotently. A completed plan starts a new inspect
and reuse pass. The endpoint accepts `{}` only; no component paths, commands or
package choices are accepted.

## Component readiness

Runtime mode includes a `components` map in status and SSE snapshots. Each item
reports identity, state, verification method, last verified time, failure code
and a safe message. Components include uv, managed Python, model manager,
backend/worker environments, FFmpeg, Silero, Whisper, TitaNet, pyannote,
runtime probes, backend service and dataset processing. States include
`pending`, `downloading`, `files_verified`, `verified`, `stopped`, `failed`,
and `blocked`. A healthy backend or verified model files do not imply inference
readiness. `partial_ready` is a development indicator only.

The future gated-model operation will use `waiting_for_user` and an
authenticated same-origin submission scoped to the active attempt. A trusted
helper must consume the credential through protected stdin/descriptor data;
it must never appear in persistent job state, events, URLs, process arguments,
logs or diagnostics. It will be discarded after the helper exits. This release
has no gated-model consumer and accepts no token.

Operation response example:

```json
{
  "job": { "id": "a676d84c-341a-4eae-9bd7-8b71dd149647", "state": "running" },
  "idempotent": false,
  "state_revision": 3
}
```

The actual `job` value uses the full job shape shown above.

### `GET /setup-api/diagnostics`

Returns the current safe job, up to 24 recent sanitized transitions, and the
state revision. It does not return environment variables, paths outside the
controller data directory, process command lines, credentials, or stack traces.

### `GET /setup-api/health`

The only unauthenticated endpoint. Returns process health only:

```json
{ "status": "ok", "controller_status": "running" }
```

It says nothing about Python, models, inference, or production readiness.

Errors use this shape:

```json
{
  "error": { "code": "RETRY_NOT_ALLOWED", "message": "Retry is allowed only for failed, cancelled, or interrupted jobs." },
  "state_revision": 18
}
```

## Progress and Server-Sent Events

`GET /setup-api/events` requires the session cookie and
`Accept: text/event-stream`. The controller immediately emits an authoritative
`snapshot`. Event IDs are monotonically increasing persisted state revisions.
Every connection ends with an authoritative full snapshot. Retained transition
frames carry a compact job projection and any changed component states in
`component_updates`; clients should read the final snapshot rather than rebuild
readiness from the journal. Example:

```text
id: 18
event: task_progress
data: {"state_revision":18,"job":{"id":"...","state":"running","stage":"install_models"},"component_updates":{"whisper":{"state":"downloading","verification":"pending"}},"simulated":false,"production_ready":false}
```

On reconnect, send the standard `Last-Event-ID` header. If all later journal
entries are retained, the controller sends those events followed by a fresh
authoritative snapshot. If the ID is invalid, ahead of current state, or older
than the retained journal, it sends the latest snapshot instead of implying
complete history. The bounded journal retains at most 256 entries. Slow clients
are disconnected rather than accumulating unbounded per-client buffers; native
EventSource reconnects and obtains current state.

Disconnecting a browser or event stream does not cancel setup. Cancellation is
an explicit API operation. The controller itself does not continue work while
stopped; unfinished `running` or `cancelling` work becomes `interrupted` on the
next startup and requires explicit retry.

## Progress contract

Progress is an object with `phase`, `current`, `total`, `percent`, and
`message`. The phase is task state (for example `downloading`, `verifying`,
`installing`, or `complete`). Byte fields and percentage are null if the
underlying operation does not expose trustworthy byte progress. The bootstrap
does not fabricate progress percentages for uv-managed Python/package work.
`bootstrap_status` tracks `uv_verified`, `python_verified`,
`model_manager_verified`, and `bootstrap_ready` separately. Paths or versions
may be added to this object for authenticated local diagnostics; clients should
ignore fields they do not use.

## Readiness boundary

Every response has `production_ready: false`, including successful runtime
plans. The `simulated` field describes the configured controller mode. Model
files are only file-verified; actual inference is not asserted.
