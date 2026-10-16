# Source-checkout launcher and runtime status

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

The executable can be exposed as the `speechcraft` command without changing system files:

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

On an already onboarded install, the launcher establishes a private controller session, requests service restoration through the authenticated API, and polls status using that session. The controller revalidates the installed model-manager package, model inventories, selected worker recipe, managed FFmpeg, and local offline runtime probes, then restores the backend without launching downloads or creating a setup job. A 401 is an immediate authentication failure; the launcher never polls protected status anonymously. The login chain displays a repair notice if current processing health is degraded; `/setup` remains available by direct navigation. Completion is sticky and does not mean current health or public-release approval. TitaNet provenance and FFmpeg redistribution compliance remain release blockers.

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

## Release preparation status

Next.js uses its supported standalone output rooted at the repository. `npm run package:standalone` copies the server and static assets without frontend `node_modules`, but first requires a passing strict TypeScript check. The current checkout has unrelated type errors in `speechcraft-write-api.ts` and a missing `react-hook-form` dependency, so the release staging command correctly refuses to package it. No application archive or user installer is produced yet.

Bundled Node metadata pins the official Node.js 24.22.0 LTS Linux x86-64 and macOS ARM64 archives to their published SHA-256 identities. `launcher/scripts/fetch-node-runtime.mjs` downloads a pinned archive, checks the digest, and extracts only fixed archive members plus the license. This is release-build tooling; no signed application artifact or per-user installer is produced by this checkout. WSL2 uses the Linux Node payload. The macOS worker, MLX Whisper, and managed macOS FFmpeg recipes remain unavailable pending trusted dependency recipes and Apple Silicon execution validation.
