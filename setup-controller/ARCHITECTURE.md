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

Backend service status says only that the API process answers its health check.
It waits at a stdin launch gate until its PID and OS identity are durably
recorded. A v2 starting intent with no PID proves the gate was never released
and is safe to clear; records without this protocol remain conservative.
The backend retains ownership of on-demand dataset jobs. It receives
`SPEECHCRAFT_DATASET_WORKER_ROOT` and
`SPEECHCRAFT_DATASET_WORKER_PYTHON`; the latter points at the future managed
worker environment, and worker processing remains unavailable until that
environment and its required tools validate.

## Current component gates

The Linux CPU worker lock is missing. The checked-in worker project/lock is
CUDA 12.8 constrained and has not been validated as an end-user recipe. The
macOS worker recipe is also missing. GPU detection is diagnostic only and
never upgrades the selected runtime profile to CUDA readiness. There is no
trusted FFmpeg/FFprobe artifact bundle or redistribution record, no real
inference validation helper, no gated pyannote source/token consumer, and
TitaNet release approval remains blocked. Consequently dataset processing and
`production_ready` remain false. Runtime installation can be partial and the
API reports each blocker.

Model status distinguishes `files_verified` from `runtime_ready`. A completed
setup plan is not a production-ready workstation. No synthetic inference
probes or experimental TitaNet override are used.

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

If gated-model auth is added, the future API must bind a one-time credential to
the active attempt and send it through protected stdin or a file descriptor to a
trusted Python helper. It must not enter state, arguments, logs or events and
must be discarded after use. This release has no such helper and never accepts
or persists HF tokens.

## Profile validation status

| Profile | Artifact selection | Bootstrap implementation | Full worker/runtime status |
|---|---|---|---|
| Linux x86-64 CPU | Linux artifacts | Shared | Blocked: worker recipe/tools/probes unavailable |
| Linux x86-64 NVIDIA | Linux artifacts | Shared | GPU may be detected; CUDA worker recipe is not validated |
| Ubuntu WSL2 x86-64 | Linux runtime, WSL model label | Shared with native-filesystem enforcement | Blocked as above; WSL hardware validation pending |
| macOS ARM64 | macOS artifacts | Shared | Backend/model route implemented; worker/inference compatibility pending Apple Silicon validation |

These are implementation and blocker descriptions, not claims of host testing.
