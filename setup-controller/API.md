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

The temporary Next.js `/setup` page implements this bridge in a server-only
`POST /setup-api/session` route. It is disabled unless
`SPEECHCRAFT_SETUP_SESSION_BRIDGE=1`, an absolute
`SPEECHCRAFT_SETUP_BOOTSTRAP_FILE`, a loopback
`SPEECHCRAFT_SETUP_CONTROLLER_URL`, and explicit
`SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS` are configured. The bridge rejects
non-loopback controller URLs, unlisted Host/Origin values, cross-site requests,
symlinked or group/world-readable bootstrap files, and nonempty request
options. Its matching `GET /setup-api/session` route forwards only the
HTTP-only session cookie to the controller and returns the in-memory CSRF
token. All other `/setup-api/*` browser calls use the same-origin Next.js
rewrite. Development setup commands and the future launcher contract are in
`frontend/SETUP_FRONTEND.md`.

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

Runtime mode installs approved Silero and platform-selected Whisper files and
checks Community-1 through `speechcraft_models`. If no complete verified local
snapshot exists, setup pauses in `waiting_for_user` until the authenticated
authorization endpoint receives a token. Backend dependencies are installed
from the trusted lock. `completed` means the selected controller plan finished,
not that every component is ready; pyannote inference must still pass its
offline runtime probe.

The controller can consume a worker profile only when its trusted runtime
manifest entry has `available: true` and `validated: true`, pinned project and
lock SHA-256 values, a Python version matching the managed runtime, a versioned
environment identity, and declared import checks. Runtime helpers are
similarly manifest-pinned by shipped path and SHA-256 and have a bounded
timeout. A helper result must be one JSON object containing
`schema_version: 1`, the matching `component`, `success: true`,
`inference_completed: true`, and a nonempty `identity`. A failed or absent
helper leaves the component pending/blocked; the controller does not synthesize
inference success. These recipe fields are internal release inputs, not API
request fields.

The earlier real `bootstrap` mode uses these ordered stages:

1. `inspect_system`
2. `install_uv`
3. `install_python`
4. `install_model_manager`
5. `validate_bootstrap`

`completed` means the selected plan finished. It does not itself establish
inference or workstation readiness. `technical_processing_ready` is derived
from current managed environments, verified tools/models, real inference
probes, and authenticated backend health. `production_ready` additionally
requires release approvals. With current TitaNet provenance, FFmpeg
redistribution, and Community-1 access gates, production readiness remains
false. `bootstrap_ready` covers only uv, managed CPython and model-manager
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

All POST operations require `Content-Type: application/json`, a session cookie,
allowed Origin, and the CSRF header. Parameterless operations accept only `{}`
(up to 16 KiB). The pyannote authorization operation is the single exception:
it accepts exactly one token field in a body capped at 4 KiB. Commands, paths,
URLs, and arbitrary options remain rejected.

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
runtime probes, backend service and dataset processing. Pyannote component states
include `waiting_for_token`, `checking_token`, `checking_model_access`,
`access_denied`, `downloading`, `files_verified`, `validating_inference`,
`runtime_ready`, `interrupted`, and `failed`. File verification and actual
inference readiness remain distinct. A healthy backend or verified model files
do not imply inference readiness. `partial_ready` is a development indicator
only.

### `GET /setup-api/models`

Requires the session cookie. Returns public state for Silero, Whisper, TitaNet,
and Community-1; it excludes local credential material and file-level reports.

```json
{
  "schema_version": 1,
  "models": [{
    "role": "diarization",
    "model_id": "pyannote/speaker-diarization-community-1",
    "display_name": "pyannote Community-1",
    "state": "waiting_for_token",
    "progress": null,
    "files_verified": false,
    "runtime_ready": false,
    "action_required": true,
    "failure_code": null,
    "message": "Accept Community-1 conditions and submit a Hugging Face read token to install this model.",
    "release_blockers": []
  }]
}
```

### `POST /setup-api/models/pyannote/authorize`

Requires the session cookie, allowed Host and Origin, JSON content type, and
`X-SpeechCraft-CSRF`. It accepts only `{"token":"hf_…"}` in a body no larger
than 4 KiB. Query-string credentials, unknown fields, duplicate fields, escaped
tokens, and malformed token shapes are rejected. The response never reflects
the credential.

```http
POST /setup-api/models/pyannote/authorize
Content-Type: application/json
Origin: http://127.0.0.1:4317
Cookie: speechcraft_setup=…
X-SpeechCraft-CSRF: …

{"token":"hf_example_token_value_not_real"}
```

Returns `202` when the same runtime setup job begins an authorization attempt,
or `200` if the component is already runtime-ready. Concurrent token operations
return `409 AUTHORIZATION_ALREADY_RUNNING`. The caller should observe `/status`
or `/events`; it should not wait for the download in the HTTP request.

```json
{
  "state": "running",
  "job_id": "a676d84c-341a-4eae-9bd7-8b71dd149647",
  "stage": "install_models",
  "attempt_id": "953f50e6-ec29-4fe9-a5b6-0d0b15d8ed8c",
  "idempotent": false,
  "state_revision": 24,
  "component": { "role": "diarization", "state": "checking_token" }
}
```

The controller accepts the token only while the existing runtime setup job is
`waiting_for_user` at `install_models`. It scopes the credential to that
attempt, sends it to the fixed manager helper over stdin, and releases the
reference when the attempt exits. It is absent from job state, event history,
SSE, diagnostics, logs, subprocess argv/environment, and temporary files. The
helper checks token identity separately from access to the exact pinned gated
repository and required file, downloads only the trusted inventory, and
verifies every file before publication. Public models remain unauthenticated.

An invalid token or missing accepted model access returns the job to
`waiting_for_user` with a safe action message. Network/integrity failures are
failed or retryable without infinite access retries. Cancellation waits for the
owned helper to stop. If the controller restarts while waiting, the waiting
state remains durable. If it restarts during acquisition, process ownership is
reconciled, the token is discarded, and a later retry asks for it again. A
complete verified local snapshot is reused without token submission.

Stable authorization error codes include `HF_TOKEN_INVALID`,
`HF_MODEL_ACCESS_REQUIRED`, `HF_TOKEN_PERMISSION_DENIED`, `HF_APPROVAL_PENDING`
(only if upstream distinguishes it), `HF_NETWORK_ERROR`, `HF_RATE_LIMITED`,
`HF_SNAPSHOT_INCOMPLETE`, `HF_SNAPSHOT_INTEGRITY_FAILED`,
`MODEL_STORAGE_INSUFFICIENT`, `PYANNOTE_RUNTIME_VALIDATION_FAILED`,
`AUTHORIZATION_ALREADY_RUNNING`, and `AUTHORIZATION_REQUIRED_AFTER_RESTART`.
When Hugging Face does not distinguish approval from access denial, the API
asks the user to accept conditions and check account access instead of guessing.

Parameterless operation response example:

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

`dataset_processing` is derived from managed Python, backend and worker
environments, managed FFmpeg, Silero, Whisper Large-v3, Community-1, speaker
purity runtime capability, and a backend health check authenticated with the
identity generated for that service launch. The first unmet prerequisite
supplies the blocker code and message. Revalidation after controller restart
clears old runtime-ready claims until current artifacts and probes pass again.
A backend exit invalidates service and processing readiness.

`technical_processing_ready` means those current technical prerequisites
pass; it is separate from public-release readiness. `production_ready` also
requires licensing, provenance, and redistribution approvals. The selected
TitaNet artifact and FFmpeg distribution still have release blockers, so
current production readiness remains false. A completed setup plan only
means its configured stages finished. The `simulated` field describes the
configured controller mode.

## First-run application routing

Authenticated `GET /setup-api/status` and SSE snapshots include `onboarding`:

```json
{"completed":false,"completed_at":null,"setup_plan":null,"profile":null}
```

The controller persists a versioned completion record only after the real
`speechcraft-runtime-v1` plan has passed technical readiness and the managed
backend has passed its per-launch identity health check. Simulation and job
completion alone never set this record. Its identity is tied to the canonical
controller data directory; corrupt or mismatched state is preserved and fails
closed. The Linux launcher opens `/setup` until this record exists, then `/login`
on later launches. Current degraded health does not erase completed onboarding;
the login chain displays a repair notice and `/setup` remains available manually.
