# Linux application package and launcher

Public download flow, supported-platform status, release trust, draft creation, and the legal publication gate are documented in [DISTRIBUTION.md](DISTRIBUTION.md). Linux x86-64 is the initial target, but there is no publicly usable bootstrap URL: the release is draft-only, and independent publisher authentication plus redistribution review are still open.

## Developer-built local package (not the public installer)

Builds produce a versioned Linux x86-64 archive and a local per-user installer. The source checkout itself is not an installed application.

```bash
node launcher/scripts/package-linux.mjs --source-revision "$(git rev-parse HEAD)" --output /absolute/path/to/speechcraft-release
cd /absolute/path/to/speechcraft-release
sha256sum -c speechcraft-0.1.0-linux-x86_64.tar.gz.sha256
./install-linux.sh speechcraft-0.1.0-linux-x86_64.tar.gz
```

The builder exports the exact requested Git commit into an isolated source snapshot and builds the frontend from that snapshot; unrelated worktree changes are not included. The frontend source and backend/worker resources therefore share one declared source revision. The build requires the checked-in frontend package and Bun lockfiles to be unchanged and a prepared frontend dependency tree whose Next.js version matches the committed package manifest. The release manifest records the source revision and dependency-lock digest; this is source provenance, not a claim of byte-for-byte reproducibility.

The installer verifies the archive checksum, rejects traversal and special archive entries, checks every packaged file and the mandatory runtime-resource inventory, then publishes a versioned read-only release under `~/.local/share/speechcraft/app/releases/`. It registers the command before atomically switching `current`; a failed activation restores the previous command and pointer. It installs `~/.local/bin/speechcraft`. If that directory is not on the current `PATH`, it adds one clearly marked, idempotent SpeechCraft block to `~/.profile` and asks you to open a fresh login terminal. It refuses to modify an unrelated command or symlinked/non-user-owned install destination. The local archive installer uses the system Python 3.9+ standard library; the running application uses only its bundled Node runtime and the controller-managed Python runtime.

After installation, from any directory:

```bash
speechcraft
speechcraft --no-browser
speechcraft status
speechcraft status --json
speechcraft stop
```

The first launch opens `/setup`. After the controller records verified onboarding completion, later launches open `/login`. `--no-browser` prints the URL and leaves services running in the terminal. On a headless server, use an SSH tunnel from your workstation:

```bash
ssh -N -L 3002:127.0.0.1:3002 user@cozytent-host
```

Then visit `http://127.0.0.1:3002/setup` locally. Controller, frontend and backend bind only to loopback. `speechcraft stop` authenticates with the controller when available and independently reconciles launcher-owned child process identities, so a crashed frontend does not prevent shutdown. It reports incomplete or uncertain ownership instead of killing an unrelated process. Ctrl+C stops services owned by that terminal; closing the browser does not stop them.

Persistent state, controller credentials, managed runtimes, models, caches, projects, recordings and exports remain under `~/.local/share/speechcraft/`. Installing an upgrade creates another versioned application directory and switches `current`; it does not remove user data or prior releases. To remove application binaries, remove `~/.local/bin/speechcraft` and `~/.local/share/speechcraft/app/`. Keep `~/.local/share/speechcraft/data/` to retain user data and models. The installer refuses to replace an unrelated `speechcraft` command and is safe to rerun for an identical release.

The packaged release includes the verified Node.js 24.22.0 Linux x86-64 executable, Next.js standalone server and traced runtime dependencies, setup controller, backend and dataset-worker resources (including the production `scripts/preflight.py` and `runtime_probe.py` entrypoints), probe fixtures, runtime manifests, and the model-manager wheel with its exact digest. The builder validates these required resources against the runtime manifest and fails before publication if one is missing. It does not include model weights, credentials, virtual environments, or caches. FFmpeg redistribution review remains a separate public-release item; WeSpeaker ECAPA-TDNN512-LM provenance and CC BY 4.0 attribution are documented; no real gated Community-1 account or model inference is implied by package smoke tests.

The package is Linux x86-64. Windows can launch that same package inside Ubuntu WSL2 through the separate Windows integration archive. Native Windows acceptance, real WSL localhost networking, and CUDA-on-WSL remain untested until `launcher/windows/accept-windows.ps1` is run on a supported PC. macOS Apple Silicon runtime validation remains separate.

## Source-checkout development

The source-checkout launch path is available on Linux x86-64 and can start the UI on Apple Silicon macOS. It requires Node.js 20.9 or newer and prepared frontend dependencies. The trusted runtime manifest still blocks the macOS worker profile, so this is not end-to-end Apple Silicon processing support. Ubuntu WSL2 uses the Linux runtime and native WSL storage; WSL1 is rejected. This checkout is not the public zero-prerequisite installer.

Prepare frontend dependencies once from the checked-in Bun lockfile, then launch from the repository root:

```bash
cd frontend && bun install --frozen-lockfile && npm run build
cd ..
./bin/speechcraft
```

For a source checkout while developing, use `./bin/speechcraft --development`. The default data root is the controller's SpeechCraft data directory. Use `--data-dir /path/in/linux/filesystem` for an isolated installation. The directory must be owned by the current user; WSL2 additionally rejects Windows-backed storage. The launcher binds Next.js, the controller, and the managed backend to `127.0.0.1`; it refuses occupied ports unless the existing frontend/controller prove they belong to the same installation.

For a real runtime setup from a source checkout, the setup controller also needs the exact model-manager wheel and its trusted digest. A developer can prepare that local input with the repository's pinned `uv` tooling, then launch with:

```bash
uv build --project packages/model_manager --wheel --out-dir /tmp/speechcraft-wheel
export SPEECHCRAFT_MODEL_MANAGER_WHEEL="$(find /tmp/speechcraft-wheel -maxdepth 1 -name 'speechcraft_model_manager-0.1.0-*.whl' -print -quit)"
export SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256="$(sha256sum "$SPEECHCRAFT_MODEL_MANAGER_WHEEL" | cut -d' ' -f1)"
./bin/speechcraft
```

The controller verifies the wheel against this supplied digest before a fresh model-manager install or repair. In a release package these values must come from signed/trusted release metadata, not an interactive shell. If they are absent during installation or repair, that operation fails with an explicit wheel requirement; the launcher does not invent a source artifact. An already installed manager environment is independently revalidated from its installed wheel metadata and trusted lock identity, so an ordinary repeat launch does not need this wheel or digest.

The source-checkout wrapper can be exposed for development without changing system files:

```bash
mkdir -p "$HOME/.local/bin"
ln -s "$(pwd)/bin/speechcraft" "$HOME/.local/bin/speechcraft"
```

On desktop Linux it opens the browser after Next.js responds. On a headless server it prints the local URL. From a laptop, use an SSH tunnel, replacing the placeholders:

```bash
ssh -N -L 3002:127.0.0.1:3002 user@cozytent-host
```

Then open `http://127.0.0.1:3002/setup`. No controller or backend port needs public exposure. Press Ctrl+C or send SIGTERM to the launcher to stop the service processes it started. Closing a browser tab leaves services running.

## First launch and later launches

The controller stores a versioned `onboarding` completion record in its atomic state store. It is created only when the real `speechcraft-runtime-v1` plan has passed the controller's technical-readiness evaluator and the supervised backend has passed its private health-identity check. Simulated setup never creates this record. An incomplete, interrupted, failed, or waiting first setup launches `/setup` on each invocation. A matching completed record launches `/login` on subsequent invocations. Corrupt or installation-mismatched state fails closed at controller startup rather than being reset.

On an already onboarded install, the launcher establishes a private controller session, requests service restoration through the authenticated API, and polls status using that session. The controller revalidates the installed model-manager package, model inventories, selected worker recipe, managed FFmpeg, and local offline runtime probes, then restores the backend without launching downloads or creating a setup job. A 401 is an immediate authentication failure; the launcher never polls protected status anonymously. The login chain displays a repair notice if current processing health is degraded; `/setup` remains available by direct navigation. Completion is sticky and does not mean current health or public-release approval. FFmpeg redistribution compliance remains a separate release review item; technical readiness is reported independently.

The launcher authenticates a reused frontend with a private random challenge identity under the data root (mode 0600). It first checks only public availability, then verifies a nonce HMAC response. It never sends the private identity to an unverified listener. The public installation ID remains diagnostic only.

## Lifecycle commands

From another terminal, use:

```bash
speechcraft status
speechcraft status --json
speechcraft stop
speechcraft --no-browser
```

`status` establishes a private controller session, verifies frontend ownership with the private nonce proof, and reports the controller's current component states. `stop` posts an authenticated, CSRF-protected shutdown request. The controller joins setup cancellation and runtime restoration, stops its owned backend, and exits; the attached launcher then stops the frontend child it owns. If termination cannot be confirmed, it reports failure instead of killing an unrelated process. Ctrl+C in the original terminal uses the same owned-child shutdown behavior. Closing the browser tab does not stop SpeechCraft.

The backend is considered healthy only while the controller reports its current supervised process instance as `verified`. Restart and failure transitions clear previous verification evidence.

The launcher does not install or download anything itself. First-run installation continues through the setup page and setup controller. The controller's runtime setup records completion only after its trusted required checks succeed, including pyannote runtime validation.

## Source checkout and isolated acceptance

The frontend must already have the pinned lockfile dependencies. Runtime mode also needs the trusted runtime resources and enough disk space. For controller/UI mechanics only, use an isolated simulated launch:

```bash
./bin/speechcraft --data-dir /tmp/speechcraft-launcher-test --development --simulated --no-browser
```

Run the local launcher-to-Next-to-controller HTTP smoke test with:

```bash
node launcher/scripts/integration.mjs
```

It verifies the loopback page, server-side session bridge, authenticated status/SSE, duplicate-launch reuse, and an onboarding record created through `SetupController` and `RuntimeTasks` using controlled verified component evidence. The latter validates the production state transition, not model inference; it is not evidence of model installation or inference. Repeat-launch fixture intentionally removes wheel path/digest inputs and checks the normal route remains available while missing runtime artifacts surface as repair blockers.

## Troubleshooting and safe diagnostics

The controller diagnostics endpoint is available in the setup page after session establishment. A port collision is not killed automatically: stop the known owner or choose a later supported launcher configuration in a future release. If shutdown reports uncertain process identity, preserve the data directory and inspect the reported process before retrying. Do not remove controller lock or service ownership records by hand while an owned process may still exist.

CozyTent first-run/model acceptance requires approved SSH access and a Hugging Face account that has accepted Community-1 conditions. Those credentials must be entered only in the masked setup page; never place them in commands or environment variables.

## Linux package validation

The single package build entrypoint is `node launcher/scripts/package-linux.mjs --source-revision <40-character-commit> --output /absolute/output/directory`. It uses `git archive` for all application source and excludes untracked files and working-tree edits. It runs strict TypeScript validation and the production Next.js build in the exported tree, stages traced standalone dependencies and static assets, builds the model-manager wheel, downloads and verifies the pinned Node runtime, checks the required backend/worker resource inventory, writes per-file identities, and emits the archive checksum. The generated archive is not committed to source control. The package script refuses an existing output directory, mismatched dependency manifests, or an incomplete required input.

The staged frontend can also be tested independently with `node launcher/scripts/standalone-smoke.mjs --frontend /absolute/stage/frontend --node /absolute/runtime/node`. After building an archive, run the actual installed application test with `node launcher/scripts/installed-linux-smoke.mjs --archive /absolute/archive.tar.gz --installer /absolute/install-linux.py`. It installs twice in a fresh temporary home, removes global Node from the service PATH, exercises HTTP setup/session/CSRF/SSE/origin behavior, checks `/proc` executable identities, and validates normal and crashed-frontend shutdown. It uses simulated setup mechanics and is not real model acquisition or inference evidence.

The installed command runs the Node binary inside the versioned application directory; it does not use global Node. The packaged backend's `dataset_worker_client` resolves `workers/dataset/scripts/preflight.py` from the installed worker root. The installed-package smoke invokes this production backend interface with a controlled interpreter and confirms that absent ML dependencies are reported as blockers instead of a missing-script or source-checkout-path failure. Fresh model installation gets the packaged wheel path and SHA-256 from the application release manifest. Ordinary launches do not reinstall the manager or download models. Real model acquisition and inference remain available through the existing setup controller, subject to model access, hardware and release policy.

## Current validation and blockers

Strict frontend typecheck and production build passed in the Linux package build. The installed package smoke test passed in a path containing spaces and from `/tmp`, with a PATH containing no global Node. It verified the default first-run `/setup` route, loopback listeners, session bridge, CSRF-protected setup action, SSE, rejected untrusted Origin, and identity-verified shutdown including a dead frontend. A normal runtime-mode first launch also served `/setup` and reported the four model roles pending. No multi-gigabyte model download or real inference was run. CozyTent access and an authorized Hugging Face account were not available in this environment. Public FFmpeg redistribution clearance remains unresolved. Native macOS and WSL2 are not included in this Linux acceptance.
