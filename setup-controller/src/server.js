import http from 'node:http';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { SessionAuth } from './auth.js';
import { ControllerError, SetupController, publicComponents, serializePublicJob } from './controller.js';
import { defaultDataDirectory } from './paths.js';
import { StateStore, StateStoreError } from './state-store.js';
import { SimulatedTasks } from './simulated-tasks.js';
import { BootstrapTasks, reconcileOwnedProcess } from './bootstrap.js';
import { RuntimeTasks } from './runtime-tasks.js';
import { reconcileOwnedService } from './backend-service.js';
import { assertCurrentWslGuest, assertExistingManagedFiles, hostEnvironment, managedStorageLocations, validateManagedStorage } from './platform.js';

const BODY_LIMIT = 16 * 1024;
const HEARTBEAT_MS = 15_000;
const SSE_CLIENT_LIMIT = 32;

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function json(response, status, value, headers = {}) {
  const body = Buffer.from(`${JSON.stringify(value)}\n`);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': body.length,
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'no-referrer',
    ...headers,
  });
  response.end(body);
}

async function readJson(request) {
  const type = (request.headers['content-type'] || '').split(';', 1)[0].trim().toLowerCase();
  if (type !== 'application/json') throw new HttpError(415, 'JSON_REQUIRED', 'Send a JSON request body.');
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > BODY_LIMIT) throw new HttpError(413, 'BODY_TOO_LARGE', 'Request body exceeds the 16 KiB limit.');
    chunks.push(chunk);
  }
  if (size === 0) return {};
  const raw = Buffer.concat(chunks).toString('utf8');
  let value;
  try { value = JSON.parse(raw); }
  catch { throw new HttpError(400, 'INVALID_JSON', 'Request body must contain valid JSON.'); }
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new HttpError(400, 'INVALID_REQUEST', 'Request body must be a JSON object.');
  }
  if (Object.keys(value).length) {
    throw new HttpError(400, 'UNSUPPORTED_FIELDS', 'This endpoint does not accept caller-supplied commands, paths, or options.');
  }
  return value;
}

async function readAuthorizationJson(request) {
  const type = (request.headers['content-type'] || '').split(';', 1)[0].trim().toLowerCase();
  if (type !== 'application/json') throw new HttpError(415, 'JSON_REQUIRED', 'Send a JSON request body.');
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 4096) throw new HttpError(413, 'BODY_TOO_LARGE', 'Authorization request exceeds the 4 KiB limit.');
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString('utf8');
  let value;
  try { value = JSON.parse(raw); }
  catch { throw new HttpError(400, 'INVALID_JSON', 'Request body must contain valid JSON.'); }
  if (!value || Array.isArray(value) || typeof value !== 'object'
      || Object.keys(value).length !== 1 || !Object.hasOwn(value, 'token')) {
    throw new HttpError(400, 'UNSUPPORTED_FIELDS', 'Send only the documented token field.');
  }
  if (!/^\s*\{\s*"token"\s*:\s*"hf_[A-Za-z0-9_-]{16,512}"\s*\}\s*$/.test(raw)) {
    throw new HttpError(400, 'INVALID_REQUEST', 'Send one unescaped ASCII token value in the documented JSON shape.');
  }
  if (typeof value.token !== 'string' || !/^hf_[A-Za-z0-9_-]{16,512}$/.test(value.token)) {
    throw new HttpError(400, 'HF_TOKEN_INVALID', 'Enter a valid Hugging Face read token.');
  }
  return value.token;
}

function publicModels(controller) {
  const components = controller.status().components || {};
  const records = [
    ['silero', 'speech_detection', 'silero-vad-onnx', 'Silero VAD ONNX'],
    ['whisper', 'asr', 'whisper-large-v3', 'Whisper Large-v3'],
    ['ecapa', 'speaker_purity', 'wespeaker-ecapa-tdnn512-lm', 'WeSpeaker ECAPA-TDNN512-LM ONNX'],
    ['pyannote', 'diarization', 'pyannote/speaker-diarization-community-1', 'pyannote Community-1'],
  ];
  return { schema_version: 1, models: records.map(([key, role, model_id, display_name]) => {
    const value = components[key] || {};
    return { role, model_id, display_name, state: value.state || 'pending',
      progress: value.progress || null, files_verified: ['files_verified', 'validating_inference', 'runtime_ready'].includes(value.state),
      runtime_ready: value.runtime_ready === true || value.state === 'runtime_ready',
      action_required: value.state === 'waiting_for_token' || value.state === 'access_denied',
      failure_code: value.failure_code || null, message: value.message || null,
      release_blockers: value.release_blockers || [] };
  }) };
}

function parseCsv(value) {
  return value?.split(',').map((item) => item.trim()).filter(Boolean) || [];
}

function statusEvent(job, revision, controllerStatus = 'running', mode = job?.mode || 'simulated', componentUpdates = null, onboarding = null) {
  const publicJob = job ? serializePublicJob(job) : {
    schema_version: 2,
    id: null, state: 'idle', stage: null, task_id: null, progress: null,
    error: null, attempt_count: 0, attempt_id: null, revision,
    created_at: null, updated_at: null, started_at: null, completed_at: null,
  };
  return { controller_status: controllerStatus, state_revision: revision, job: publicJob,
    mode, simulated: mode === 'simulated', setup_plan: job?.plan_id || null,
    components: publicComponents(job?.bootstrap_status?.components),
    component_updates: publicComponents(componentUpdates),
    runtime_profile: publicJob?.bootstrap_status?.runtime_profile || null,
    bootstrap_ready: job?.bootstrap_status?.bootstrap_ready === true,
    partial_ready: job?.bootstrap_status?.partial_ready === true,
    technical_processing_ready: job?.bootstrap_status?.technical_processing_ready === true,
    production_ready: job?.bootstrap_status?.production_ready === true,
    onboarding: onboarding?.completed ? { completed: true, completed_at: onboarding.completed_at, profile: onboarding.profile } : { completed: false } };
}

function sse(response, id, event, data) {
  const encoded = `id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  return response.write(encoded);
}

function diagnostics(store, mode = 'simulated') {
  const state = store.snapshot();
  return {
    state_revision: state.revision,
    current_job: serializePublicJob(state.job),
    recent_transitions: state.events.slice(-24).map((event) => ({
      id: event.id,
      type: event.type,
      at: event.at,
      state: event.data.job?.state || 'idle',
      stage: event.data.job?.stage || null,
      progress: event.data.job?.progress || null,
      error: event.data.job?.error || null,
      component_updates: publicComponents(event.data.component_updates),
    })),
    mode,
    simulated: mode === 'simulated',
    production_ready: state.job?.bootstrap_status?.production_ready === true,
  };
}

function publicMutation(result, revision) {
  return { job: serializePublicJob(result.job), idempotent: result.idempotent === true,
    state_revision: revision };
}

export async function createSetupController({
  dataDirectory = defaultDataDirectory(),
  port = 4317,
  host = '127.0.0.1',
  allowedHosts,
  allowedOrigins,
  secureCookies = false,
  tasks,
  simulated = {},
  mode = 'simulated',
  bootstrapOptions = {},
} = {}) {
  if (host !== '127.0.0.1') throw new Error('setup controller must bind to IPv4 loopback (127.0.0.1)');
  if (!['simulated', 'bootstrap', 'runtime'].includes(mode)) throw new Error('setup mode must be simulated, bootstrap, or runtime');
  if (mode !== 'simulated') {
    const environment = hostEnvironment();
    if (environment.wsl) assertCurrentWslGuest(environment);
    validateManagedStorage(environment, managedStorageLocations(dataDirectory));
    assertExistingManagedFiles(environment, [
      path.join(dataDirectory, 'bootstrap.token'),
      path.join(dataDirectory, 'launcher.processes.json'),
    ]);
  }
  const store = StateStore.open(dataDirectory);
  let auth;
  let controller;
  const clients = new Set();
  let stopping = false;
  let hosts = [];
  let origins = [];
  let unsubscribe = () => {};
  let heartbeat;
  let closePromise;

  try {
    auth = new SessionAuth(store.directory, { secureCookies });
    const executor = tasks || (mode !== 'simulated'
      ? (mode === 'runtime' ? new RuntimeTasks({ dataDirectory: store.directory, store, ...bootstrapOptions })
        : new BootstrapTasks({ dataDirectory: store.directory, ...bootstrapOptions }))
      : new SimulatedTasks(simulated));
    // Ownership reconciliation is mode-independent: a simulated restart must
    // not bypass a writer left by an interrupted real bootstrap.
    await reconcileOwnedProcess(store);
    await reconcileOwnedService(store);
    if (executor.recoverInterrupted) await executor.recoverInterrupted(store);
    else if (['bootstrap', 'runtime'].includes(store.snapshot().job?.mode)) {
      // A later simulation-only process still revalidates persisted real
      // artifacts. It never retries or installs, but does not retain stale
      // readiness claims merely because the launcher changed its mode.
      await new BootstrapTasks({ dataDirectory: store.directory, ...bootstrapOptions }).recoverInterrupted(store);
    }
    controller = new SetupController(store, { tasks: executor, mode });
    const server = http.createServer((request, response) => {
      void route(request, response).catch((error) => {
        if (response.headersSent) {
          response.destroy();
          return;
        }
        const status = error instanceof ControllerError || error instanceof HttpError
          ? error.status : 500;
        const code = error instanceof ControllerError || error instanceof HttpError
          ? error.code : 'INTERNAL_ERROR';
        const message = status === 500 ? 'The setup controller could not complete the request.' : error.message;
        json(response, status, { error: { code, message }, state_revision: store.snapshot().revision });
      });
    });

    function validateHostOrigin(request, { originRequired = false } = {}) {
      const requestHost = request.headers.host?.toLowerCase();
      if (!requestHost || !hosts.includes(requestHost)) {
        throw new HttpError(400, 'INVALID_HOST', 'Request Host is not allowed.');
      }
      const origin = request.headers.origin;
      if (originRequired && !origin) throw new HttpError(403, 'ORIGIN_REQUIRED', 'A same-origin request is required.');
      if (origin && !origins.includes(origin)) throw new HttpError(403, 'ORIGIN_REJECTED', 'Request Origin is not allowed.');
      if (request.headers['sec-fetch-site'] === 'cross-site') {
        throw new HttpError(403, 'CROSS_SITE_REJECTED', 'Cross-site requests are not allowed.');
      }
    }

    function requireSession(request) {
      const session = auth.sessionFromRequest(request);
      if (!session) throw new HttpError(401, 'AUTH_REQUIRED', 'Establish a local setup session first.');
      return session;
    }

    function requireCsrf(request, session) {
      if (!auth.csrfMatches(session, request.headers['x-speechcraft-csrf'])) {
        throw new HttpError(403, 'CSRF_REJECTED', 'The request CSRF token is missing or invalid.');
      }
    }

    function publishToClients(event, state) {
      const currentStatus = controller.status();
      const data = statusEvent(event.data.job, event.id, currentStatus.controller_status, currentStatus.mode, event.data.component_updates || null, currentStatus.onboarding);
      for (const client of clients) {
        if (!sse(client, event.id, event.type, data)) {
          clients.delete(client);
          client.end();
        }
      }
    }

    function openEvents(request, response) {
      if (clients.size >= SSE_CLIENT_LIMIT) {
        throw new HttpError(503, 'EVENT_CLIENT_LIMIT', 'Too many setup event clients are connected.');
      }
      const state = store.snapshot();
      response.writeHead(200, {
        'content-type': 'text/event-stream; charset=utf-8',
        'cache-control': 'no-cache, no-transform',
        connection: 'keep-alive',
        'x-accel-buffering': 'no',
        'x-content-type-options': 'nosniff',
      });
      response.flushHeaders();
      const lastRaw = request.headers['last-event-id'];
      const last = typeof lastRaw === 'string' && /^\d+$/.test(lastRaw) ? Number(lastRaw) : null;
      const events = state.events;
      const oldest = events[0]?.id ?? state.revision;
      const replayPossible = last !== null && Number.isSafeInteger(last)
        && last <= state.revision && last >= oldest - 1;
      if (replayPossible && last < state.revision) {
        for (const event of events) {
          if (event.id > last && !sse(response, event.id, event.type,
            statusEvent(event.data.job, event.id, 'running', controller.status().mode, event.data.component_updates || null, controller.status().onboarding))) break;
        }
      }
      const currentStatus = controller.status();
      sse(response, state.revision, 'snapshot', statusEvent(state.job, state.revision,
        currentStatus.controller_status, currentStatus.mode, null, currentStatus.onboarding));
      clients.add(response);
      const onClose = () => clients.delete(response);
      response.once('close', onClose);
    }

    async function route(request, response) {
      const url = new URL(request.url, 'http://controller.invalid');
      if (url.search) throw new HttpError(400, 'QUERY_NOT_ALLOWED', 'Setup API credentials and options are never accepted in URLs.');
      validateHostOrigin(request);
      if (url.pathname === '/setup-api/health') {
        if (request.method !== 'GET') throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET for health.');
        json(response, 200, { status: stopping ? 'stopping' : 'ok',
          controller_status: stopping ? 'stopping' : 'running' });
        return;
      }
      if (!url.pathname.startsWith('/setup-api/')) {
        throw new HttpError(404, 'NOT_FOUND', 'Unknown setup API endpoint.');
      }
      if (stopping && request.method !== 'GET') {
        throw new HttpError(503, 'CONTROLLER_STOPPING', 'The setup controller is stopping.');
      }

      if (url.pathname === '/setup-api/session') {
        if (request.method === 'POST') {
          validateHostOrigin(request, { originRequired: true });
          await readJson(request);
          if (!auth.bootstrapMatches(request.headers.authorization)) {
            throw new HttpError(401, 'BOOTSTRAP_REJECTED', 'The launcher session credential is invalid.');
          }
          const session = auth.establish(request.headers.authorization);
          json(response, 201, { csrf_token: session.csrf_token, expires_at: session.expires_at },
            { 'set-cookie': session.set_cookie });
          return;
        }
        if (request.method === 'GET') {
          const session = requireSession(request);
          json(response, 200, { csrf_token: session.csrf, expires_at: new Date(session.expiresAt).toISOString() });
          return;
        }
        throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET or POST for session establishment.');
      }

      const session = requireSession(request);
      if (url.pathname === '/setup-api/status') {
        if (request.method !== 'GET') throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET for status.');
        json(response, 200, controller.status());
        return;
      }
      if (url.pathname === '/setup-api/models') {
        if (request.method !== 'GET') throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET for model status.');
        json(response, 200, publicModels(controller));
        return;
      }
      if (url.pathname === '/setup-api/diagnostics') {
        if (request.method !== 'GET') throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET for diagnostics.');
        json(response, 200, diagnostics(store, controller.mode));
        return;
      }
      if (url.pathname === '/setup-api/events') {
        if (request.method !== 'GET') throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Use GET for events.');
        if (request.headers.accept && !request.headers.accept.includes('text/event-stream')) {
          throw new HttpError(406, 'SSE_REQUIRED', 'Request events with Accept: text/event-stream.');
        }
        openEvents(request, response);
        return;
      }
      if (!['POST'].includes(request.method)) {
        throw new HttpError(405, 'METHOD_NOT_ALLOWED', 'Only documented setup API methods are allowed.');
      }
      validateHostOrigin(request, { originRequired: true });
      requireCsrf(request, session);
      if (url.pathname === '/setup-api/models/pyannote/authorize') {
        const token = await readAuthorizationJson(request);
        const result = controller.authorizeModel('diarization', token);
        const job = result.job;
        json(response, result.idempotent ? 200 : 202, {
          job: serializePublicJob(job), state: job.state, job_id: job.id, stage: job.stage, attempt_id: job.attempt_id,
          idempotent: result.idempotent, state_revision: store.snapshot().revision,
          component: publicModels(controller).models.find((model) => model.role === 'diarization'),
        });
        return;
      }
      await readJson(request);
      if (url.pathname === '/setup-api/start') {
        const result = controller.start();
        json(response, result.idempotent ? 200 : 202, publicMutation(result, store.snapshot().revision));
        return;
      }
      if (url.pathname === '/setup-api/retry') {
        const result = controller.retry();
        json(response, 202, publicMutation(result, store.snapshot().revision));
        return;
      }
      if (url.pathname === '/setup-api/repair') {
        const current = store.snapshot().job;
        const result = current && ['failed', 'cancelled', 'interrupted'].includes(current.state)
          ? controller.retry() : controller.start();
        json(response, result.idempotent ? 200 : 202, publicMutation(result, store.snapshot().revision));
        return;
      }
      if (url.pathname === '/setup-api/cancel') {
        const result = controller.cancel();
        json(response, result.idempotent ? 200 : 202, publicMutation(result, store.snapshot().revision));
        return;
      }
      if (url.pathname === '/setup-api/services/restore' || url.pathname === '/setup-api/services/restore/retry') {
        if (controller.mode !== 'runtime' || !store.snapshot().onboarding?.completed) {
          throw new HttpError(409, 'RESTORE_NOT_APPLICABLE', 'Managed service restoration is available only for a completed runtime installation.');
        }
        if (typeof executor.restoreManagedServices !== 'function') {
          throw new HttpError(503, 'BACKEND_RESTORE_UNAVAILABLE', 'This controller cannot restore managed services for the installed runtime.');
        }
        // Startup and launcher requests share RuntimeTasks' single in-flight
        // restoration promise. This endpoint only asks the existing trusted
        // executor to restore verified services; it never creates an install
        // job or starts model acquisition.
        const retry = url.pathname.endsWith('/retry');
        void executor.restoreManagedServices({ retry }).catch(() => {});
        json(response, 202, { accepted: true, state: 'restoring', state_revision: store.snapshot().revision });
        return;
      }
      if (url.pathname === '/setup-api/shutdown') {
        json(response, 202, { accepted: true, state: 'stopping', state_revision: store.snapshot().revision });
        // The response must be flushed before closing the listener. close()
        // joins setup cancellation, runtime restoration, owned subprocesses,
        // and the supervised backend before releasing installation ownership.
        setImmediate(() => { void close().catch(() => {}); });
        return;
      }
      throw new HttpError(404, 'NOT_FOUND', 'Unknown setup API endpoint.');
    }

    unsubscribe = store.subscribe(publishToClients);
    heartbeat = setInterval(() => {
      for (const client of clients) {
        if (!client.write(': keep-alive\n\n')) {
          clients.delete(client);
          client.end();
        }
      }
    }, HEARTBEAT_MS);
    heartbeat.unref();

    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen({ host, port, exclusive: true }, resolve);
    });
    if (mode === 'runtime' && store.snapshot().onboarding?.completed
        && typeof executor.restoreManagedServices === 'function') {
      // Recovery is intentionally separate from setup jobs and never installs
      // or downloads. The public API remains available while trusted local
      // probes and service restoration run in the background.
      setImmediate(() => { void executor.restoreManagedServices().catch(() => {}); });
    }
    const address = server.address();
    const actualPort = typeof address === 'object' ? address.port : port;
    hosts = allowedHosts || [`127.0.0.1:${actualPort}`, `localhost:${actualPort}`];
    origins = allowedOrigins || [`http://127.0.0.1:${actualPort}`, `http://localhost:${actualPort}`];

    function close() {
      if (closePromise) return closePromise;
      stopping = true;
      closePromise = (async () => {
        await controller.shutdown();
        await executor.shutdown?.();
        clearInterval(heartbeat);
        unsubscribe();
        for (const client of clients) client.end();
        clients.clear();
        await new Promise((resolve) => server.close(resolve));
        auth.close();
        store.releaseOwnership();
      })();
      return closePromise;
    }

    return { server, controller, store, auth, host, port: actualPort,
      dataDirectory: store.directory, bootstrapPath: auth.bootstrapPath, close };
  } catch (error) {
    clearInterval(heartbeat);
    unsubscribe();
    for (const client of clients) client.end();
    try { auth?.close(); } catch {}
    try { store.releaseOwnership(); } catch {}
    throw error;
  }
}

function parseArgs(args) {
  const options = { port: Number(process.env.SPEECHCRAFT_SETUP_PORT || 4317),
    dataDirectory: defaultDataDirectory(), mode: process.env.SPEECHCRAFT_SETUP_MODE || 'simulated' };
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === '--port' && args[index + 1]) options.port = Number(args[++index]);
    else if (argument === '--data-dir' && args[index + 1]) options.dataDirectory = path.resolve(args[++index]);
    else if (argument === '--mode' && args[index + 1]) options.mode = args[++index];
    else if (argument === '--step-delay-ms' && args[index + 1]) options.simulated = { stepDelayMs: Number(args[++index]) };
    else throw new Error(`unknown or incomplete argument: ${argument}`);
  }
  if (!Number.isInteger(options.port) || options.port < 0 || options.port > 65535) throw new Error('port must be an integer from 0 to 65535');
  if (!['simulated', 'bootstrap', 'runtime'].includes(options.mode)) throw new Error('mode must be simulated, bootstrap, or runtime');
  if (options.simulated && (!Number.isInteger(options.simulated.stepDelayMs) || options.simulated.stepDelayMs < 0 || options.simulated.stepDelayMs > 60_000)) {
    throw new Error('step delay must be an integer from 0 to 60000 milliseconds');
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const allowedHosts = parseCsv(process.env.SPEECHCRAFT_SETUP_ALLOWED_HOSTS);
  const allowedOrigins = parseCsv(process.env.SPEECHCRAFT_SETUP_ALLOWED_ORIGINS);
  const app = await createSetupController({
    ...options,
    bootstrapOptions: {
      wheelPath: process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL,
      wheelSha256: process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256,
      applicationRoot: process.env.SPEECHCRAFT_APPLICATION_ROOT,
    },
    allowedHosts: allowedHosts.length ? allowedHosts.map((value) => value.toLowerCase()) : undefined,
    allowedOrigins: allowedOrigins.length ? allowedOrigins : undefined,
    secureCookies: process.env.SPEECHCRAFT_SETUP_SECURE_COOKIES === '1',
  });
  console.log(JSON.stringify({ event: 'listening', host: app.host, port: app.port,
    bootstrap_file: app.bootstrapPath, mode: app.controller.mode,
    simulated: app.controller.mode === 'simulated' }));
  let closing = false;
  const stop = async (signal) => {
    if (closing) return;
    closing = true;
    console.log(JSON.stringify({ event: 'shutdown', signal }));
    await app.close();
  };
  process.once('SIGINT', () => { void stop('SIGINT').then(() => process.exit(0)); });
  process.once('SIGTERM', () => { void stop('SIGTERM').then(() => process.exit(0)); });
}

if (import.meta.url === pathToFileURL(process.argv[1] || '').href) {
  main().catch((error) => {
    const message = error instanceof StateStoreError ? `${error.code}: ${error.message}`
      : error instanceof Error ? error.message : 'controller startup failed';
    console.error(`setup controller startup failed: ${message}`);
    process.exitCode = 1;
  });
}
