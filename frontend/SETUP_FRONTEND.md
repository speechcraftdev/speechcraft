# Preparing SpeechCraft frontend

The temporary setup page is `/setup`, outside the login-chain and workstation route groups. It consumes the existing Node controller API and does not store Hugging Face credentials. The page can be revisited to inspect or repair an installation.

## Local development

Use Node.js 20.9 or newer (the controller and Next.js both run under Node; no Python is needed to display or operate simulated setup). Start the controller and frontend in separate terminals.

Choose a private persistent development data directory, then start the controller in simulated mode:

```sh
mkdir -m 700 -p "$HOME/.local/share/speechcraft-setup-dev"
export SPEECHCRAFT_SETUP_ALLOWED_HOSTS='127.0.0.1:4317,localhost:4317,127.0.0.1:3002,localhost:3002'
export SPEECHCRAFT_SETUP_ALLOWED_ORIGINS='http://127.0.0.1:4317,http://localhost:4317,http://127.0.0.1:3002,http://localhost:3002'
node setup-controller/src/server.js --data-dir "$HOME/.local/share/speechcraft-setup-dev" --port 4317 --mode simulated --step-delay-ms 500
```

In a second terminal, explicitly enable the local Next.js session bridge and run the frontend:

```sh
export SPEECHCRAFT_SETUP_SESSION_BRIDGE=1
export SPEECHCRAFT_SETUP_CONTROLLER_URL='http://127.0.0.1:4317'
export SPEECHCRAFT_SETUP_BOOTSTRAP_FILE="$HOME/.local/share/speechcraft-setup-dev/bootstrap.token"
export SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS='http://127.0.0.1:3002,http://localhost:3002'
npm --prefix frontend run dev
```

Open `http://127.0.0.1:3002/setup`. Simulated mode exercises session bootstrap, state display, SSE, cancellation and controller recovery only. It does not install models or validate inference. Do not configure a runtime controller with a fake token or claim simulated completion as model readiness.

The bridge reads its controller URL, bootstrap file, and allowed application origins only from server configuration. It accepts only loopback HTTP controller URLs, validates the application Host and Origin, rejects cross-site requests, requires an owner-only regular `bootstrap.token` in a private directory, and forwards only the session cookie and CSRF response. The browser never receives the bearer bootstrap credential. Do not put any of these settings in `NEXT_PUBLIC_*` variables.

For a launcher or trusted reverse proxy, provide equivalent private server-side configuration. Keep the controller bound to IPv4 loopback; allow the frontend's exact browser origins plus the controller origin in the controller's Host/Origin lists; preserve browser Origin, `/setup-api` path, cookies and CSRF headers; set `SPEECHCRAFT_SETUP_SECURE_COOKIES=1` when HTTPS is used. The launcher must own the private bootstrap capability and must not expose it in a URL, page JavaScript, logs, or arbitrary website requests. The bridge is not a remote administration endpoint; only configure origins and hosts for a trusted local same-origin frontend.

## API client behavior

`src/lib/setup-api.ts` is the typed browser client. It keeps CSRF state in module memory, re-fetches the session after page refresh, and re-establishes a session after controller restart. Authenticated REST calls use relative `/setup-api/*` paths. SSE uses one reconnecting fetch stream with event revisions; the page refetches the authoritative status and model list after reconnect and also polls at a low rate if the stream is unavailable. Progress is shown only when supplied by the controller.

The Community-1 form posts the token only to `/setup-api/models/pyannote/authorize`. It uses a password field, does not persist the value, and clears the input after an accepted response. Error text is selected by stable error code and never echoes the submitted token.

During gated acquisition, the controller waits for a positive durable subprocess-ownership acknowledgment and a current-attempt confirmation before sending credential bytes over private stdin. Mutable controller/Python input buffers are cleared after consumption or once the `install_models` stage settles. Ordinary immutable JavaScript strings cannot be promised immediate erasure; the implementation avoids persistent storage, unnecessary logging and copies.

Check the actual API schema in [setup-controller/API.md](../setup-controller/API.md). `/login3` remains the run-specific preflight route; its existing Whisper Base fallback needs separate laptop acceptance follow-up and was not changed in this page task.

## Verification

```sh
npm --prefix frontend run typecheck
npm --prefix frontend run build
npm --prefix frontend run test:setup-api
npm --prefix setup-controller test
```

`test:setup-api` launches a real simulated controller and Next.js dev server on ports 4317 and 3002. It verifies session establishment, host-only HttpOnly cookie behavior, CSRF-protected mutation, same-origin proxying, SSE updates, cross-origin rejection, missing credential/controller handling, refresh and controller restart recovery. It needs those ports to be available.
