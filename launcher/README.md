# Linux source-checkout launcher

This is the local Linux x86-64 launch path. It requires Node.js 20.9 or newer, the exact frontend dependencies in `frontend/package.json`, and either a prepared Next.js build or an explicit development launch. It is not the public zero-prerequisite installer and does not bundle Node or frontend assets.

Prepare frontend dependencies once, then launch from the repository root:

```bash
cd frontend && npm ci && npm run build
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

CozyTent first-run/model acceptance requires approved SSH access and a Hugging Face account that has accepted Community-1 conditions. Those credentials must be entered only in the masked setup page; never place them in commands or environment variables. The public release archive, bundled Node, and curl installer are not part of this source-checkout launcher.
