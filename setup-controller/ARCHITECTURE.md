# Setup controller architecture and readiness

## Ownership

The Node controller is the sole owner of setup orchestration. Its filesystem
lock permits one controller to mutate an installation at a time. Setup jobs
have stable IDs and per-attempt identities; callbacks from prior attempts are
ignored. The state file is authoritative and includes a bounded event journal.
The controller never requires Python to start.

Install subprocesses use the persisted process-group ownership protocol in
`bootstrap.js`. Cancellation is terminal only after the group is confirmed
stopped. A separate `service_process` record tracks the long-lived FastAPI
backend, so a healthy server is not confused with an unfinished installer.
Startup reconciles both records. Unidentifiable service/install ownership
blocks startup rather than risking concurrent writers. A controller crash
cannot automatically continue work; it leaves an interrupted job for explicit
retry.

## Trusted recipes and paths

`resources/bootstrap-manifest.json` pins uv, managed CPython and the internal
model-manager wheel/dependency bootstrap contract. `resources/runtime-manifest.json`
records backend and worker recipe identities plus explicitly unavailable
profiles. The backend plan verifies the repository `pyproject.toml` and
`uv.lock` SHA-256 values before locked installation. `--no-install-project`
keeps versioned dependencies reusable while the running controller imports
backend code only from its current application release.

Managed runtimes, environments and caches live below the controller's
SpeechCraft data directory, never within versioned application releases.
Backend environments are versioned by runtime/profile/lock identity. A failed
new environment is quarantined; prior versions remain intact. Setup cleanup
does not inspect projects, recordings, exports or model directories.

Models are stored in `<data>/models`, outside release directories. The Python
`speechcraft_models` CLI remains the sole model downloader/verifier. HF home,
Hub and Xet caches are explicitly set below `<data>/cache/bootstrap`; on WSL,
the existing mount-aware storage policy rejects Windows-backed paths even when
Linux artifact selection is used. Verified model files survive application
updates and are reused through the model manager's status/install checks.

## Runtime plan

`bootstrap` remains for compatibility and verifies uv, managed Python and the
lightweight model manager. Launcher-selected `runtime` extends this plan with:

1. Locked backend dependency environment installation and import validation.
2. Worker recipe consumption: verify the declared project and lock hashes,
   install into a versioned environment with the managed interpreter, then
   validate the declared imports and environment identity. The stage reports
   unavailable while no profile recipe is explicitly marked validated.
3. Silero and platform-selected Whisper acquisition through the model manager.
4. Runtime-probe execution when trusted helpers are declared. Helpers must be
   in the shipped application tree, match their manifest SHA-256, finish under
   a bounded timeout, and return schema-versioned JSON with
   `inference_completed: true`. Imports alone do not produce runtime readiness.
5. Backend startup in a process group, loopback `/healthz` validation and
   separate service ownership.

Backend service readiness requires the private `/_setup/health` route to echo
the unpredictable identity for that launch over loopback. A generic listener
on the selected port or a stale identity is rejected. The identity is passed
only to the child process and internal health request; it is not persisted or
included in public setup responses. The public `/healthz` route remains a
basic process health endpoint.
It waits at a stdin launch gate until its PID and OS identity are durably
recorded. A v2 starting intent with no PID proves the gate was never released
and is safe to clear; records without this protocol remain conservative.
The backend retains ownership of on-demand dataset jobs. It receives
`SPEECHCRAFT_DATASET_WORKER_ROOT` and
`SPEECHCRAFT_DATASET_WORKER_PYTHON`; the latter points at the verified,
versioned managed worker environment. Dataset processing remains blocked
until its required tools, models, inference probes, and service checks pass.

## Current component gates

The controller and model manager support authenticated Community-1 acquisition
for the pinned `pyannote/speaker-diarization-community-1` revision. When no
complete local snapshot exists, runtime setup waits for a user token. The
authenticated controller endpoint sends that credential only over a private
stdin pipe after PID and process identity are durably recorded. The helper
checks identity and gated file access separately. Mutable credential buffers
are cleared after use where practical; credentials are not persisted. The
offline probe consumes a real pyannote `Annotation` through the same
`annotation_to_speechcraft_regions` converter as the production pipeline and
requires two in-bounds speaker identities from the pinned CC0 two-voice
fixture. This does not replace validation on human multi-speaker recordings.
An authorized snapshot is still required to execute the probe.

The Linux CPU recipe and the host-specific Linux CUDA route are documented in
`workers/dataset/RUNTIME_PROFILES.md`; the manifest contains the current
validated recipes. macOS runtime validation remains pending Kavin's hardware
test. Managed FFmpeg/FFprobe is available and locally verified, while product
redistribution review remains a release blocker. TitaNet provenance remains
blocked, and Community-1 requires an authorized snapshot plus the real offline
probe. The API reports technical and release blockers separately.

Model status distinguishes `files_verified` from `runtime_ready`. A completed
setup plan is not a production-ready workstation. No placeholder inference or
automatic experimental TitaNet override is used.

## Authentication handoff

The future launcher must read `bootstrap.token` only in its trusted process and
call the controller's authenticated session bootstrap privately over loopback.
The same-origin Next.js proxy should accept a credential-free browser request,
perform that bootstrap itself, and forward only the HTTP-only session cookie and
CSRF token. The secret must never enter a URL, browser JavaScript, page source,
console output or persistent job/event data. For SSH tunnels, keep the controller
on loopback and explicitly configure expected Host/Origin at the trusted proxy.
Origin, Host and CSRF checks remain active. Launcher/proxy integration still
needs end-to-end validation.

The current authenticated API accepts the token only at
`POST /setup-api/models/pyannote/authorize`, requires same-origin and CSRF
checks, and scopes it to the active setup attempt. The credential is piped to
the fixed model-manager helper only after durable process ownership is
established. It is not written to state, argv, environment, logs, events,
diagnostics, or temporary files. A controller restart during acquisition
discards the token and asks for it again; a complete verified local snapshot
does not require reauthorization.

## Profile validation status

| Profile | Artifact selection | Bootstrap implementation | Full worker/runtime status |
|---|---|---|---|
| Linux x86-64 CPU | Linux artifacts | Shared; CPU worker lock is available | Environment and CPU probes documented as exercised; full Community-1 processing is pending authorized local model access |
| Linux x86-64 NVIDIA | Linux artifacts | Shared; CUDA 12.8 recipe | Hardware-specific results and current `libcublas` status are recorded in `workers/dataset/RUNTIME_PROFILES.md`; revalidation is per installation |
| Ubuntu WSL2 x86-64 | Linux runtime, WSL model label | Shared with native-filesystem enforcement | Runtime uses Linux profile; native WSL host validation remains pending |
| macOS ARM64 | macOS artifacts | Shared | Apple Silicon worker/inference compatibility pending Kavin's validation |

These are implementation and blocker descriptions, not claims of host testing.
