# Setup Controller API v1

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

States are `idle`, `running`, `waiting_for_user`, `failed`, `cancelled`,
`interrupted`, and `completed`. `idle` is the snapshot state when no job exists;
the other values belong to a persisted job. Legal transitions are enforced by
the controller. Retry is an explicit new attempt on the existing job ID.

This milestone simulates these ordered stages:

1. `inspect_system`
2. `bootstrap_runtime`
3. `install_dependencies`
4. `install_models`
5. `validate_runtime`

No stage installs or inspects production runtimes. `completed` means only that
the simulated stages finished.

## Snapshot and job shape

`GET /setup-api/status` returns the current authoritative snapshot. It requires
the session cookie. Example while running:

```json
{
  "controller_status": "running",
  "job": {
    "schema_version": 1,
    "id": "a676d84c-341a-4eae-9bd7-8b71dd149647",
    "state": "running",
    "stage": "install_models",
    "task_id": "simulated:install_models",
    "created_at": "2026-10-14T10:30:00.000Z",
    "updated_at": "2026-10-14T10:30:02.000Z",
    "started_at": "2026-10-14T10:30:00.000Z",
    "completed_at": null,
    "attempt_count": 1,
    "revision": 18,
    "progress": {
      "current": 1,
      "total": 2,
      "percent": 50,
      "message": "install_models simulation in progress"
    },
    "error": null
  },
  "state_revision": 18,
  "simulated": true,
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

Starts the simulated job. Returns `202` with the new job, or `200` with the
existing job when it is already running/waiting. A terminal existing job returns
`409 JOB_EXISTS`; use retry when allowed.

### `POST /setup-api/retry`

Retries the same job ID from `failed`, `cancelled`, or `interrupted`. The attempt
count increases. Returns `202`. Other states return `409 RETRY_NOT_ALLOWED`.

### `POST /setup-api/cancel`

Persists cancellation and aborts owned simulated work. Returns `202` when it
cancels a job and `200` for repeated cancellation of the same job. A completed
or failed job cannot be cancelled.

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
Progress and transition frames carry the same public snapshot shape:

```text
id: 18
event: task_progress
data: {"state_revision":18,"job":{"id":"...","state":"running","stage":"install_models"},"simulated":true,"production_ready":false}
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
stopped; an unfinished `running` job becomes `interrupted` on the next startup
and requires explicit retry.

## Simulated readiness boundary

Every response has `simulated: true` and `production_ready: false`, including a
successful job. No inference or production readiness is asserted by this API.
