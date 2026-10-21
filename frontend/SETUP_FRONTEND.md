# SpeechCraft model onboarding frontend

The user-facing setup page has been removed. The launcher opens `/login`, which
continues to `/login3` for model preflight and download, then `/login2` for
recording import. The existing `/setup-api/*` controller endpoints remain
private same-origin APIs used by model onboarding; they are not a standalone
setup page.

## Local development

Use Node.js 20.9 or newer. The launcher starts the frontend and the private
setup controller together. For direct frontend development, use the repository
launcher so its authenticated controller bridge and runtime configuration are
available:

```sh
./bin/speechcraft --development
```

Open `http://127.0.0.1:3002/login`. The model screen reports only controller
and download progress that it can verify. The setup controller remains bound
to loopback and must not be exposed through a public tunnel.

## Controller API

`src/lib/setup-api.ts` is the typed browser client. It restores the authenticated
session after refresh and uses `/setup-api/*` through the same browser origin.
The private launcher bridge keeps the bootstrap credential server-side. Keep
the controller on loopback and preserve the existing Host, Origin, cookie, and
CSRF checks. Do not place controller credentials in `NEXT_PUBLIC_*` variables.

The Community-1 verification flow posts the token only to
`/setup-api/models/pyannote/preauthorize`; Verify checks access and never starts
setup. The controller keeps the verified token in memory, bound to the current
setup session for at most five minutes. The explicit Download Models action
starts or resumes setup and consumes that token. Refresh or controller restart
returns the user to verification because credentials are never persisted.

See [setup-controller/API.md](../setup-controller/API.md) for the API schema.
