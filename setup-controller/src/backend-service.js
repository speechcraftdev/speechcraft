import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import net from 'node:net';
import fs from 'node:fs';
import path from 'node:path';
import { ownedProcessIdentity, terminateRecoveredProcess } from './bootstrap.js';
import { validateManagedStorage } from './platform.js';

const START_TIMEOUT_MS = 30_000;
const POLL_MS = 250;
const CHILD_CLOSE_CONFIRM_MS = 2_000;

function childCloseConfirmed(child) {
  return Boolean(child?.closed || child?.exitCode !== null || child?.signalCode !== null);
}

function waitForChildClose(child, timeoutMs = CHILD_CLOSE_CONFIRM_MS) {
  if (childCloseConfirmed(child)) return Promise.resolve(true);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (closed) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.removeListener('close', onClose);
      resolve(closed || childCloseConfirmed(child));
    };
    const onClose = () => finish(true);
    const timer = setTimeout(() => finish(false), timeoutMs);
    child.once('close', onClose);
    // Close may have fired between the initial check and listener install.
    if (childCloseConfirmed(child)) finish(true);
  });
}

function freeLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close((error) => error ? reject(error) : resolve(port));
    });
  });
}

async function health(port, timeout = 800) {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(timeout) });
    if (!response.ok) return false;
    const value = await response.json();
    return value?.status === 'ok';
  } catch { return false; }
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new Error('cancelled')); return; }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, ms);
    const abort = () => { clearTimeout(timer); reject(new Error('cancelled')); };
    signal.addEventListener('abort', abort, { once: true });
  });
}

export class BackendService {
  constructor({ store, dataDirectory, applicationRoot, environment, pythonPath, environmentVars, attemptId, workerPython = null, workerRoot = null,
    onStatus = () => {}, onOutput = () => {} }) {
    this.store = store;
    this.dataDirectory = dataDirectory;
    this.applicationRoot = applicationRoot;
    this.environment = environment;
    this.pythonPath = pythonPath;
    this.environmentVars = environmentVars;
    this.attemptId = attemptId;
    this.workerPython = workerPython;
    this.workerRoot = workerRoot;
    this.onStatus = onStatus;
    this.onOutput = onOutput;
    this.child = null;
    this.record = null;
    this.port = null;
    this.stopping = false;
    this.restartCount = 0;
  }

  async start(signal) {
    if (this.record && this.child && this.child.exitCode === null && this.child.signalCode === null
        && await health(this.port)) return { port: this.port, reused: true };
    if (this.record) await this.stop();
    const backendRoot = path.join(this.applicationRoot, 'backend');
    let executable;
    let managedRuntimeRoot;
    let managedEnvironmentRoot;
    let requestedExecutable;
    try {
      requestedExecutable = path.resolve(this.pythonPath);
      executable = fs.realpathSync(requestedExecutable);
      managedRuntimeRoot = fs.realpathSync(path.join(this.dataDirectory, 'runtimes'));
      managedEnvironmentRoot = fs.realpathSync(path.join(this.dataDirectory, 'environments'));
    }
    catch { throw Object.assign(new Error('The verified managed backend interpreter is unavailable.'), { code: 'BACKEND_ENVIRONMENT_INVALID' }); }
    const lexicalRelative = path.relative(managedEnvironmentRoot, requestedExecutable);
    const runtimeRelative = path.relative(managedRuntimeRoot, executable);
    const environmentRelative = path.relative(managedEnvironmentRoot, executable);
    const info = fs.statSync(requestedExecutable);
    if (!info.isFile() || lexicalRelative === '..' || lexicalRelative.startsWith(`..${path.sep}`)
        || path.isAbsolute(lexicalRelative)
        || (runtimeRelative === '..' || runtimeRelative.startsWith(`..${path.sep}`) || path.isAbsolute(runtimeRelative))
          && (environmentRelative === '..' || environmentRelative.startsWith(`..${path.sep}`) || path.isAbsolute(environmentRelative))
        || (info.mode & 0o111) === 0) throw Object.assign(new Error('The backend interpreter did not resolve to a SpeechCraft-managed runtime.'), { code: 'BACKEND_ENVIRONMENT_INVALID' });
    const appData = path.join(this.dataDirectory, 'application-data');
    const projectData = path.join(appData, 'projects');
    validateManagedStorage(this.environment, [appData, projectData,
      path.join(projectData, 'media'), path.join(projectData, 'exports')]);
    const port = await freeLoopbackPort();
    const nonce = randomUUID();
    const attemptId = this.attemptId || 'service';
    const record = { kind: 'speechcraft-backend', state: 'starting', launch_protocol: 2, pid: null,
      process_group_id: null, identity: null, nonce, attempt_id: attemptId, port,
      created_at: new Date().toISOString(), termination_status: 'active' };
    await this.persist(record);
    const env = { ...this.environmentVars, SPEECHCRAFT_DB_PATH: path.join(appData, 'projects', 'speechcraft.db'),
      SPEECHCRAFT_MEDIA_ROOT: path.join(appData, 'projects', 'media'),
      SPEECHCRAFT_EXPORTS_ROOT: path.join(appData, 'projects', 'exports'),
      SPEECHCRAFT_DATASET_WORKER_ROOT: this.workerRoot || path.join(this.applicationRoot, 'workers', 'dataset'),
      SPEECHCRAFT_DATASET_WORKER_PYTHON: this.workerPython || '',
      SPEECHCRAFT_OWNED_PROCESS_NONCE: nonce };
    // The service cannot bind its port or write application data until its
    // durable PID/identity record has been committed. If the controller dies
    // before that commit, closing stdin makes this gate exit harmlessly.
    const script = `import sys\nif sys.stdin.buffer.read(1) != b'1': raise SystemExit(125)\nimport uvicorn\nsys.path.insert(0,${JSON.stringify(backendRoot)})\nuvicorn.run('app.main:app',host='127.0.0.1',port=${port},log_level='warning')`;
    // Keep the venv path as argv[0]: CPython uses it to locate sys.prefix.
    // `executable` above is only the resolved target used for containment.
    const child = spawn(requestedExecutable, ['-I', '-c', script], { cwd: backendRoot, env, shell: false,
      detached: true, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    this.port = port;
    child.stdout.on('data', (chunk) => this.onOutput('stdout', chunk));
    child.stderr.on('data', (chunk) => this.onOutput('stderr', chunk));
    await new Promise((resolve, reject) => {
      child.once('error', reject);
      child.once('spawn', resolve);
    }).catch(async (error) => {
      await this.persist(null);
      throw Object.assign(new Error('The managed FastAPI backend could not be started.'), { code: 'BACKEND_START_FAILED', cause: error });
    });
    record.pid = child.pid;
    record.process_group_id = child.pid;
    record.identity = ownedProcessIdentity(child.pid, nonce);
    if (!record.identity) {
      this.record = record;
      await this.stop();
      throw Object.assign(new Error('The backend process identity could not be established safely.'), { code: 'BACKEND_PROCESS_IDENTITY_UNKNOWN' });
    }
    record.state = 'running';
    this.record = record;
    try {
      await this.persist(record);
      child.stdin.end('1');
    } catch (error) {
      // The gate has not been released, so no backend writer can run. Closing
      // stdin causes the child to exit without touching application state.
      child.stdin.destroy();
      const closed = await waitForChildClose(child);
      if (!closed) {
        // Keep the durable launch intent and process reference. A later
        // controller must reconcile this gated child before another launch.
        throw Object.assign(new Error('Backend launch ownership could not be persisted and its gated child has not exited; retry is blocked until reconciliation.'), {
          code: 'BACKEND_LAUNCH_TERMINATION_UNCERTAIN', cause: error,
        });
      }
      throw Object.assign(new Error('Could not persist backend process ownership before launch.'), { code: 'BACKEND_OWNERSHIP_PERSIST_FAILED', cause: error });
    }
    child.once('exit', () => { void this.handleExit(record); });
    try {
      const deadline = Date.now() + START_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (signal.aborted) throw Object.assign(new Error('Backend startup was cancelled.'), { code: 'BOOTSTRAP_CANCELLED' });
        if (child.exitCode !== null || child.signalCode !== null) throw Object.assign(new Error('The backend exited before its health check succeeded.'), { code: 'BACKEND_START_FAILED' });
        if (await health(port)) {
          this.onStatus({ state: 'verified', verification: 'loopback_healthz', last_verified_at: new Date().toISOString(), failure_code: null, message: null, port }, record);
          return { port, reused: false };
        }
        await sleep(POLL_MS, signal);
      }
      throw Object.assign(new Error('The backend did not become healthy before the startup timeout.'), { code: 'BACKEND_HEALTH_TIMEOUT' });
    } catch (error) {
      await this.stop().catch(() => {});
      throw error;
    }
  }

  async verify() {
    const ok = this.port !== null && await health(this.port);
    if (!ok) throw Object.assign(new Error('The managed backend health endpoint is not responding.'), { code: 'BACKEND_UNHEALTHY' });
    return { port: this.port, health: 'ok' };
  }

  async persist(record) {
    this.record = record;
    await this.store.commit((state) => { state.service_process = record; }, record ? 'backend_process_started' : 'backend_process_stopped');
  }

  async clear() { this.record = null; this.port = null; this.child = null; await this.persist(null); }

  async stop() {
    if (!this.record) return;
    this.stopping = true;
    const record = this.record;
    try { await terminateRecoveredProcess(record); }
    catch (error) { this.stopping = false; throw error; }
    if (this.child && this.child.pid === record.pid) {
      if (!await waitForChildClose(this.child)) {
        this.stopping = false;
        throw Object.assign(new Error('The backend process group stopped, but the child close event was not confirmed; ownership is retained.'), { code: 'BACKEND_CHILD_CLOSE_UNCONFIRMED' });
      }
    }
    await this.clear();
    this.stopping = false;
    this.onStatus({ state: 'stopped', verification: 'termination_confirmed', last_verified_at: new Date().toISOString(), failure_code: null, message: null }, record);
  }

  async handleExit(record) {
    if (this.stopping || this.record?.pid !== record.pid) return;
    if (this.store.snapshot().service_process?.state !== 'running') return;
    if (this.restartCount >= 1) {
      this.onStatus({ state: 'failed', failure_code: 'BACKEND_EXITED', message: 'The managed backend exited unexpectedly after its one automatic restart.' }, record);
      return;
    }
    this.restartCount += 1;
    this.onStatus({ state: 'restarting', failure_code: 'BACKEND_EXITED', message: 'The backend exited unexpectedly; one supervised restart is being attempted.' }, record);
    queueMicrotask(async () => {
      // Reconciliation by a replacement controller can clear the durable
      // ownership record while this process still has an exit callback queued.
      // Do not resurrect a service that the authoritative store has stopped.
      if (this.stopping || this.record?.pid !== record.pid
          || this.store.snapshot().service_process?.pid !== record.pid
          || this.store.snapshot().service_process?.state !== 'running') return;
      try {
        const result = await this.start(new AbortController().signal);
        this.onStatus({ state: 'verified', verification: 'loopback_healthz', last_verified_at: new Date().toISOString(), failure_code: null, message: 'Backend recovered after one bounded restart.', port: result.port }, this.record);
      } catch {
        this.onStatus({ state: 'failed', failure_code: 'BACKEND_RESTART_FAILED', message: 'The backend did not recover after its one automatic restart.' }, this.record || record);
      }
    });
  }
}

export async function reconcileOwnedService(store) {
  const record = store.snapshot().service_process;
  if (!record) return { status: 'none' };
  if (record.pid === null && record.state === 'starting' && record.launch_protocol === 2) {
    // Protocol 2 children wait for a one-byte stdin release that is sent only
    // after PID and process identity are durably recorded. No release means
    // the process could not have started the backend.
    store.commit((state) => {
      if (state.service_process?.nonce === record.nonce) state.service_process = null;
      const component = state.job?.bootstrap_status?.components?.backend_service;
      if (component) {
        component.state = 'stopped'; component.verification = 'launch_gate_not_released';
        component.failure_code = 'BACKEND_RESTART_REQUIRED';
        component.message = 'A controller restart interrupted backend launch before its durable ownership gate; retry service startup.';
        state.job.bootstrap_status.production_ready = false;
      }
    }, 'backend_unreleased_launch_reconciled');
    return { status: 'launch_not_released' };
  }
  store.commit((state) => {
    if (state.service_process?.pid === record.pid) {
      state.service_process.state = 'stopping';
      state.service_process.termination_status = 'termination_pending';
    }
  }, 'backend_process_reconciliation_started');
  try {
    await terminateRecoveredProcess(record);
  } catch (error) {
    store.commit((state) => {
      if (state.service_process?.pid === record.pid) {
        state.service_process.state = 'termination_uncertain';
        state.service_process.termination_status = 'termination_uncertain';
      }
    }, 'backend_process_termination_uncertain');
    throw error;
  }
  store.commit((state) => {
    state.service_process = null;
    const component = state.job?.bootstrap_status?.components?.backend_service;
    if (component) {
      component.state = 'stopped';
      component.verification = 'termination_confirmed';
      component.failure_code = 'BACKEND_RESTART_REQUIRED';
      component.message = 'The controller restarted and stopped its previous backend process. Start services again to verify health.';
      state.job.bootstrap_status.production_ready = false;
    }
  }, 'backend_process_reconciled');
  return { status: 'termination_confirmed' };
}
