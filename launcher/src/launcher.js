import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { defaultDataDirectory } from '../../setup-controller/src/paths.js';
import { ownedProcessIdentity, terminateRecoveredProcess } from '../../setup-controller/src/bootstrap.js';
import { assertCurrentWslGuest, assertExistingManagedFiles, freeBytes, hostEnvironment, managedStorageLocations, probeLoopback, probeProcessGroup, sanitizeWslProcessEnvironment, validateManagedStorage, validateManagedStorageTargets, WSL_LAUNCH_FREE_BYTES } from '../../setup-controller/src/platform.js';
import { selectRuntimeProfile } from '../../setup-controller/src/runtime-plan.js';
import { createLaunchLogSession, formatLaunchAccess, launchOutputMode } from './launch-log.js';
import { runLinuxUninstall } from './uninstall-linux.js';
import { runMacUninstall } from './uninstall-macos.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const FRONTEND_PORT = 3002;
const CONTROLLER_PORT = 4317;
const BACKEND_PORT = 8010;
const LOOPBACK = '127.0.0.1';
const STARTUP_TIMEOUT_MS = 45_000;
const RESTORATION_TIMEOUT_MS = 10 * 60_000;
const STOP_TIMEOUT_MS = 8_000;

function bounded(promise, timeoutMs, onTimeout) {
  let timer;
  return Promise.race([promise, new Promise((resolve, reject) => {
    timer = setTimeout(() => {
      try { resolve(onTimeout()); } catch (error) { reject(error); }
    }, timeoutMs);
  })]).finally(() => clearTimeout(timer));
}

export function parseArgs(args) {
  const result = { command: 'start', dataDirectory: null, development: false, simulated: false, openBrowser: true, json: false, verbose: false, yes: false, purge: false };
  let index = 0;
  if (['status', 'stop', 'uninstall'].includes(args[0])) result.command = args[index++];
  for (; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === '--data-dir' && args[index + 1]) result.dataDirectory = path.resolve(args[++index]);
    else if (arg === '--development') result.development = true;
    else if (arg === '--simulated') result.simulated = true;
    else if (arg === '--no-browser') result.openBrowser = false;
    else if (arg === '--verbose' && result.command === 'start') result.verbose = true;
    else if (arg === '--json' && result.command === 'status') result.json = true;
    else if ((arg === '--yes' || arg === '--force') && result.command === 'uninstall') result.yes = true;
    else if ((arg === '--purge' || arg === '--remove-data') && result.command === 'uninstall') result.purge = true;
    else if (arg === '--prove-frontend' && args[index + 1] && /^[A-Za-z0-9_-]{16,128}$/.test(args[index + 1])) {
      result.proveFrontend = args[++index];
    }
    else if (arg === '--help' || arg === '-h') result.help = true;
    else throw new Error(`Unknown launcher option: ${arg}`);
  }
  return result;
}

export const FRONTEND_READY_PROTOCOL = 1;

export function chooseDestination(onboarding) {
  return onboarding?.completed === true ? '/login' : '/setup';
}

export function formatFrontendReadyEvent({ url, route, ready, owned = false }) {
  if (route !== '/setup' && route !== '/login') throw new Error('Refusing to publish an untrusted frontend route.');
  if (url !== `http://127.0.0.1:3002${route}`) throw new Error('Refusing to publish an untrusted frontend URL.');
  return `SPEECHCRAFT_READY ${JSON.stringify({
    event: 'frontend_ready', protocol: FRONTEND_READY_PROTOCOL, url, route, ready: ready === true, owned: owned === true,
  })}`;
}

export function parseFrontendReadyEvent(line) {
  if (typeof line !== 'string' || !line.startsWith('SPEECHCRAFT_READY ')) return null;
  let payload;
  try { payload = JSON.parse(line.slice('SPEECHCRAFT_READY '.length)); } catch { return null; }
  if (!payload || payload.event !== 'frontend_ready' || payload.protocol !== FRONTEND_READY_PROTOCOL || payload.ready !== true) return null;
  if ((payload.route !== '/setup' && payload.route !== '/login') || payload.url !== `http://127.0.0.1:3002${payload.route}`) return null;
  const allowed = new Set(['event', 'protocol', 'url', 'route', 'ready', 'owned']);
  if (Object.keys(payload).some((key) => !allowed.has(key))) return null;
  return { event: payload.event, protocol: payload.protocol, url: payload.url, route: payload.route, ready: true };
}

export function resolveLaunchContext({ dataDirectory = null, env = {}, host, home, mountInfoText } = {}) {
  const launchEnv = host?.wsl ? sanitizeWslProcessEnvironment(env, { mountInfoText }) : { ...env };
  const platform = host?.os === 'macos' ? 'darwin' : 'linux';
  const selected = dataDirectory || launchEnv.SPEECHCRAFT_SETUP_DATA_DIR
    || defaultDataDirectory(launchEnv, platform, home, { wsl: Boolean(host?.wsl) });
  return { dataDirectory: path.resolve(selected), env: launchEnv };
}

export function assertLoopbackAddress(address) {
  if (address !== LOOPBACK) throw new Error('SpeechCraft local services must bind explicitly to 127.0.0.1.');
}

function packageVersion(filename, field) {
  const value = JSON.parse(fs.readFileSync(filename, 'utf8'))[field];
  if (typeof value !== 'string' || !value) throw new Error(`Could not verify ${field} in ${filename}.`);
  return value;
}

function verifySourceCheckout(development) {
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 20 || (major === 20 && minor < 9)) throw new Error('SpeechCraft source launcher requires Node.js 20.9 or newer.');
  const standaloneServer = path.join(ROOT, 'frontend/server.js');
  if (fs.existsSync(standaloneServer) && fs.statSync(standaloneServer).isFile()) {
    return { nextEntry: standaloneServer, command: 'standalone', cwd: path.join(ROOT, 'frontend') };
  }
  const frontendPackage = path.join(ROOT, 'frontend/package.json');
  const nextManifest = path.join(ROOT, 'frontend/node_modules/next/package.json');
  if (!fs.existsSync(frontendPackage) || !fs.existsSync(nextManifest)) {
    throw new Error('Prepared Next.js dependencies are missing. Run `npm ci` in frontend/ before launching.');
  }
  const declared = JSON.parse(fs.readFileSync(frontendPackage, 'utf8')).dependencies?.next;
  const installed = packageVersion(nextManifest, 'version');
  if (declared !== installed) throw new Error(`Prepared frontend dependency mismatch: Next.js declares ${declared}, installed ${installed}. Install the locked frontend dependencies first.`);
  const nextEntry = path.join(ROOT, 'frontend/node_modules/next/dist/bin/next');
  if (!fs.existsSync(nextEntry) || !fs.statSync(nextEntry).isFile()) throw new Error('The prepared local Next.js executable is missing. Run the frontend dependency setup first.');
  const buildId = path.join(ROOT, 'frontend/.next/BUILD_ID');
  if (!development && (!fs.existsSync(buildId) || !fs.statSync(buildId).isFile())) {
    throw new Error('The prepared frontend build is missing. Run `npm run build` in frontend/, or use --development for a source-checkout development launch.');
  }
  return { nextEntry, command: development ? 'dev' : 'start', cwd: path.join(ROOT, 'frontend') };
}

function isPortAvailable(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', (error) => error.code === 'EADDRINUSE' ? resolve(false) : reject(error));
    server.listen(port, LOOPBACK, () => server.close((error) => error ? reject(error) : resolve(true)));
  });
}

function launcherOwnershipPath(dataDirectory) { return path.join(dataDirectory, 'launcher.processes.json'); }

export function persistLauncherOwnership(dataDirectory, handles, { identityResolver = ownedProcessIdentity,
  processGroupResolver = processGroupExists,
  processResolver = (pid) => { try { process.kill(pid, 0); return true; } catch (error) { if (error.code === 'ESRCH') return false; if (error.code === 'EPERM') return true; throw error; } },
  timeoutMs = 5_000 } = {}) {
  const target = launcherOwnershipPath(dataDirectory);
  const lock = `${target}.lock`;
  const deadline = Date.now() + timeoutMs;
  let lockFd;
  while (lockFd === undefined) {
    try { lockFd = fs.openSync(lock, 'wx', 0o600); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (Date.now() >= deadline) throw Object.assign(new Error('Another launcher is reconciling SpeechCraft process ownership.'), { code: 'PROCESS_OWNERSHIP_BUSY' });
      // A lock is removed only after its owner PID is positively absent. If
      // the PID exists (including possible PID reuse), preserve the lock.
      try {
        const owner = JSON.parse(fs.readFileSync(lock, 'utf8'));
        if (Number.isSafeInteger(owner.pid) && owner.pid > 1) {
          try { process.kill(owner.pid, 0); }
          catch (probe) {
            if (probe.code === 'ESRCH') { fs.unlinkSync(lock); continue; }
          }
        }
      } catch (readError) {
        if (readError.code === 'ENOENT') continue;
      }
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 25);
    }
  }
  try {
    fs.writeFileSync(lockFd, JSON.stringify({ pid: process.pid, nonce: crypto.randomUUID() }));
    fs.fsyncSync(lockFd);
    const existing = readLauncherOwnership(dataDirectory);
    const byPid = new Map();
    for (const record of existing) {
      const identity = identityResolver(record.pid, record.nonce);
      const groupAlive = processGroupResolver(record.pid);
      const processAlive = processResolver(record.pid);
      if (!groupAlive && !processAlive) continue;
      if (!groupAlive) throw Object.assign(new Error(`Cannot reconcile ${record.label}; its recorded process group is gone while its PID remains alive.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      if (!identity || !(record.identity === identity || record.identity.startsWith(`${identity}:`))) {
        throw Object.assign(new Error(`Cannot reconcile existing ${record.label} ownership; its process identity is uncertain.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      }
      byPid.set(record.pid, record);
    }
    for (const handle of handles) {
      const identity = handle.identityValue;
      if (!Number.isSafeInteger(handle.child.pid) || typeof handle.nonce !== 'string' || typeof identity !== 'string') {
        throw Object.assign(new Error(`Cannot persist incomplete ownership for ${handle.label}.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      }
      const currentIdentity = identityResolver(handle.child.pid, handle.nonce);
      if (!processGroupResolver(handle.child.pid) || !currentIdentity
          || (identity !== currentIdentity && !identity.startsWith(`${currentIdentity}:`))) {
        throw Object.assign(new Error(`Cannot confirm the current process identity for ${handle.label}.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      }
      const record = { label: handle.label, pid: handle.child.pid, nonce: handle.nonce,
        identity, process_group_id: handle.child.pid };
      const prior = byPid.get(record.pid);
      if (prior && (prior.identity !== record.identity || prior.nonce !== record.nonce)) {
        throw Object.assign(new Error(`Conflicting verified ownership records exist for ${handle.label}.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      }
      const conflictingLabel = [...byPid.values()].find((item) => item.label === record.label && item.pid !== record.pid);
      if (conflictingLabel) throw Object.assign(new Error(`A different verified ${record.label} process is already owned.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
      byPid.set(record.pid, record);
    }
    const records = [...byPid.values()];
    const temporary = `${target}.${crypto.randomUUID()}.tmp`;
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(fd, `${JSON.stringify({ schema_version: 1, records })}\n`, 'utf8'); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    fs.renameSync(temporary, target);
    const directoryFd = fs.openSync(dataDirectory, 'r');
    try { fs.fsyncSync(directoryFd); } finally { fs.closeSync(directoryFd); }
  } finally {
    fs.closeSync(lockFd);
    try { fs.unlinkSync(lock); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

function readLauncherOwnership(dataDirectory) {
  const filename = launcherOwnershipPath(dataDirectory);
  let info;
  try { info = fs.lstatSync(filename); } catch (error) { if (error.code === 'ENOENT') return []; throw error; }
  if (!info.isFile() || info.isSymbolicLink() || (typeof process.getuid === 'function' && info.uid !== process.getuid())
      || (info.mode & 0o077) !== 0 || info.size > 16_384 || fs.realpathSync(filename) !== filename) {
    throw Object.assign(new Error('SpeechCraft process ownership record is unsafe; no process was stopped.'), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  }
  const value = JSON.parse(fs.readFileSync(filename, 'utf8'));
  if (value.schema_version !== 1 || !Array.isArray(value.records)) throw new Error('SpeechCraft process ownership record is invalid.');
  return value.records.map((record) => {
    if (!Number.isSafeInteger(record.pid) || record.pid < 2 || typeof record.nonce !== 'string'
        || typeof record.identity !== 'string' || record.process_group_id !== record.pid) throw new Error('SpeechCraft process ownership record is incomplete.');
    return record;
  });
}

function processGroupExists(pid) {
  try { process.kill(-pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; if (error.code === 'EPERM') return true; throw error; }
}

export async function stopOwnedRecord(record, { identityResolver = ownedProcessIdentity, signalGroup = (pid, signal) => process.kill(-pid, signal), timeoutMs = STOP_TIMEOUT_MS } = {}) {
  const matches = () => {
    const actual = identityResolver(record.pid, record.nonce);
    return Boolean(actual && (record.identity === actual || record.identity.startsWith(`${actual}:`)));
  };
  const actual = identityResolver(record.pid, record.nonce);
  if (!actual) {
    if (!processGroupExists(record.pid)) return 'already_stopped';
    throw Object.assign(new Error(`${record.label} process identity is uncertain; it was not signalled.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  }
  if (!(record.identity === actual || record.identity.startsWith(`${actual}:`))) {
    throw Object.assign(new Error(`${record.label} process identity changed; it was not signalled.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  }
  try { signalGroup(record.pid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  const wait = async () => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (!processGroupExists(record.pid)) return true;
      if (!matches()) return false;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return !processGroupExists(record.pid);
  };
  if (await wait()) return 'stopped';
  if (!matches()) throw Object.assign(new Error(`${record.label} leader exited but its process group remains; ownership is uncertain.`), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  try { signalGroup(record.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  if (await wait()) return 'stopped';
  throw Object.assign(new Error(`${record.label} did not stop; ownership is retained.`), { code: 'SHUTDOWN_INCOMPLETE' });
}

async function waitFor(url, predicate, timeoutMs = STARTUP_TIMEOUT_MS, signal = null, processHandle = null) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw Object.assign(new Error('SpeechCraft startup was interrupted; stopping owned services.'), { code: 'LAUNCH_CANCELLED' });
    if (processHandle && (processHandle.child.exitCode !== null || processHandle.child.signalCode !== null)) {
      throw new Error(`SpeechCraft ${processHandle.label} exited before becoming ready (${processHandle.child.signalCode || processHandle.child.exitCode}).`);
    }
    try {
      const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(1_500) });
      if (await predicate(response)) return response;
    } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timed out waiting for local service ${url}: ${lastError?.message || 'service did not become ready'}`);
}

function readBootstrapCredential(dataDirectory) {
  const filename = path.join(dataDirectory, 'bootstrap.token');
  const file = fs.lstatSync(filename);
  const directory = fs.lstatSync(dataDirectory);
  if (!file.isFile() || file.isSymbolicLink() || (typeof process.getuid === 'function' && file.uid !== process.getuid())
      || (file.mode & 0o077) !== 0 || file.size > 128
      || !directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o077) !== 0
      || fs.realpathSync(filename) !== filename) throw new Error('The controller bootstrap credential is not a private regular file.');
  const bytes = fs.readFileSync(filename);
  const token = bytes.toString('ascii').trim();
  bytes.fill(0);
  if (!/^[A-Za-z0-9_-]{40,64}$/.test(token)) throw new Error('The controller bootstrap credential has an invalid format.');
  return token;
}

export function readOrCreateFrontendIdentity(dataDirectory) {
  const filename = path.join(dataDirectory, 'frontend.identity');
  try {
    const info = fs.lstatSync(filename);
    if (!info.isFile() || info.isSymbolicLink() || (typeof process.getuid === 'function' && info.uid !== process.getuid())
        || (info.mode & 0o077) !== 0 || info.size !== 44 || fs.realpathSync(filename) !== filename) {
      throw new Error('The private frontend identity file is unsafe.');
    }
    const identity = fs.readFileSync(filename, 'ascii').trim();
    if (!/^[A-Za-z0-9_-]{43}$/.test(identity)) throw new Error('The private frontend identity file is malformed.');
    return identity;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const identity = crypto.randomBytes(32).toString('base64url');
  const temporary = path.join(dataDirectory, `.frontend.identity-${crypto.randomUUID()}`);
  const descriptor = fs.openSync(temporary, 'wx', 0o600);
  try { fs.writeFileSync(descriptor, `${identity}\n`, 'ascii'); fs.fsyncSync(descriptor); }
  finally { fs.closeSync(descriptor); }
  try { fs.linkSync(temporary, filename); }
  catch (error) {
    fs.unlinkSync(temporary);
    if (error.code === 'EEXIST') return readOrCreateFrontendIdentity(dataDirectory);
    throw error;
  }
  fs.unlinkSync(temporary);
  return identity;
}

async function getControllerSession(dataDirectory) {
  const origin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const token = readBootstrapCredential(dataDirectory);
  try {
    const session = await fetch(`${origin}/setup-api/session`, { method: 'POST', headers: {
      host: `${LOOPBACK}:${CONTROLLER_PORT}`, origin, 'content-type': 'application/json', authorization: `Bearer ${token}`,
    }, body: '{}', cache: 'no-store', signal: AbortSignal.timeout(2_000) });
    if (!session.ok) throw new Error('The existing controller does not own this SpeechCraft data directory.');
    const cookie = session.headers.get('set-cookie')?.split(';', 1)[0];
    if (!cookie) throw new Error('The local controller did not establish its private session.');
    const csrf = await session.json();
    if (typeof csrf.csrf_token !== 'string') throw new Error('The local controller session response was invalid.');
    const response = await fetch(`${origin}/setup-api/status`, { headers: { host: `${LOOPBACK}:${CONTROLLER_PORT}`, cookie }, cache: 'no-store', signal: AbortSignal.timeout(2_000) });
    if (response.status === 401) throw Object.assign(new Error('The setup controller rejected its newly established launcher session.'), { code: 'CONTROLLER_AUTH_FAILED' });
    if (!response.ok) throw Object.assign(new Error(`The authenticated controller status request failed with HTTP ${response.status}.`), { code: 'CONTROLLER_STATUS_FAILED' });
    return { cookie, csrf: csrf.csrf_token, status: await response.json() };
  } finally {
    // JavaScript strings cannot be reliably zeroed; keep the secret short-lived
    // and never emit it to logs, arguments, or persistent launcher state.
  }
}

async function requestServiceRestore(session, { retry = false } = {}) {
  const origin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const route = retry ? '/setup-api/services/restore/retry' : '/setup-api/services/restore';
  const response = await fetch(`${origin}${route}`, { method: 'POST', headers: {
    host: `${LOOPBACK}:${CONTROLLER_PORT}`, origin, cookie: session.cookie,
    'content-type': 'application/json', 'x-speechcraft-csrf': session.csrf,
  }, body: '{}', cache: 'no-store', signal: AbortSignal.timeout(2_000) });
  if (response.status === 401) throw Object.assign(new Error('The setup controller session expired before service restoration.'), { code: 'CONTROLLER_AUTH_FAILED' });
  if (!response.ok) throw Object.assign(new Error(`The controller refused managed-service restoration (HTTP ${response.status}).`), { code: 'BACKEND_RESTORE_FAILED' });
  const result = await response.json();
  if (result.accepted !== true) throw Object.assign(new Error('The controller did not acknowledge managed-service restoration.'), { code: 'BACKEND_RESTORE_FAILED' });
  return result;
}

async function getAuthenticatedStatus(session) {
  const origin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const response = await fetch(`${origin}/setup-api/status`, { headers: {
    host: `${LOOPBACK}:${CONTROLLER_PORT}`, cookie: session.cookie,
  }, cache: 'no-store', signal: AbortSignal.timeout(2_000) });
  if (response.status === 401) throw Object.assign(new Error('The controller session expired while checking restored services.'), { code: 'CONTROLLER_AUTH_FAILED' });
  if (!response.ok) throw Object.assign(new Error(`Authenticated controller status failed with HTTP ${response.status}.`), { code: 'CONTROLLER_STATUS_FAILED' });
  return response.json();
}

async function controllerCliStatus(dataDirectory) {
  const origin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  try {
    const health = await fetch(`${origin}/setup-api/health`, { cache: 'no-store', signal: AbortSignal.timeout(700) });
    if (!health.ok) return { running: false, controller: 'unavailable', frontend: 'unavailable', url: `http://${LOOPBACK}:${FRONTEND_PORT}` };
  } catch { return { running: false, controller: 'unavailable', frontend: 'unavailable', url: `http://${LOOPBACK}:${FRONTEND_PORT}` }; }
  const session = await getControllerSession(dataDirectory);
  const value = session.status;
  let frontend = 'unavailable';
  try {
    const identity = fs.readFileSync(path.join(dataDirectory, 'frontend.identity'), 'ascii').trim();
    await verifyFrontendOwnership(`http://${LOOPBACK}:${FRONTEND_PORT}`, identity);
    frontend = 'verified';
  } catch {}
  const backend = value.components?.backend_service;
  return {
    running: frontend === 'verified',
    url: `http://${LOOPBACK}:${FRONTEND_PORT}${chooseDestination(value.onboarding)}`,
    frontend,
    controller: value.controller_status || 'running',
    backend: backend?.state === 'verified' && backend?.verification === 'internal_health_identity' ? 'healthy' : backend?.state || 'unavailable',
    onboarding_completed: value.onboarding?.completed === true,
    technical_processing_ready: value.technical_processing_ready === true,
    production_ready: value.production_ready === true,
    repair_blockers: value.components?.dataset_processing?.blockers || [],
    failure_code: backend?.failure_code || value.job?.error?.code || null,
  };
}

function printCliStatus(status, asJson = false) {
  if (asJson) { console.log(JSON.stringify(status, null, 2)); return; }
  console.log(`SpeechCraft: ${status.running ? 'running' : 'not running'}`);
  console.log(`Frontend: ${status.frontend || 'unavailable'}`);
  console.log(`Controller: ${status.controller || 'unavailable'}`);
  console.log(`Backend: ${status.backend || 'unavailable'}`);
  console.log(`Initial setup: ${status.onboarding_completed ? 'completed' : 'incomplete'}`);
  console.log(`Processing: ${status.technical_processing_ready ? 'ready' : 'not ready'}`);
  console.log(`Public release: ${status.production_ready ? 'cleared' : 'not cleared'}`);
  console.log(`URL: ${status.url}`);
  for (const blocker of status.repair_blockers || []) console.log(`Repair: ${blocker.component}: ${blocker.message}`);
  if (status.failure_code) console.log(`Failure: ${status.failure_code}`);
}

async function stopManagedApplication(dataDirectory) {
  const origin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const result = { stopped: false, status: 'shutdown_requested', services: { controller: 'not_running', frontend: 'not_running', backend: 'unknown' } };
  const failures = [];
  let records = [];
  try { records = readLauncherOwnership(dataDirectory); }
  catch (error) { failures.push(error); }
  let controllerPresent = false;
  try {
    const health = await fetch(`${origin}/setup-api/health`, { cache: 'no-store', signal: AbortSignal.timeout(700) });
    controllerPresent = health.ok;
  } catch {}
  const controllerRecord = records.find((record) => record.label === 'setup controller');
  const frontendRecord = records.find((record) => record.label === 'Next.js frontend');
  let backendRecord = null;
  let backendReportedVerified = false;
  let controllerShutdownAccepted = false;
  if (controllerPresent) {
    // Frontend health is deliberately not a prerequisite for stopping a broken application.
    let session = null;
    try { session = await getControllerSession(dataDirectory); }
    catch (error) { failures.push(Object.assign(error, { code: error.code || 'CONTROLLER_AUTH_FAILED' })); }
    try { backendRecord = readBackendProcessRecord(dataDirectory); }
    catch (error) { failures.push(error); }
    backendReportedVerified = session?.status.components?.backend_service?.state === 'verified';
    result.services.backend = backendRecord ? 'running' : backendReportedVerified ? 'ownership_unavailable' : 'not_running';
    if (session) {
      try {
        const response = await fetch(`${origin}/setup-api/shutdown`, { method: 'POST', headers: {
          host: `${LOOPBACK}:${CONTROLLER_PORT}`, origin, cookie: session.cookie,
          'content-type': 'application/json', 'x-speechcraft-csrf': session.csrf,
        }, body: '{}', cache: 'no-store', signal: AbortSignal.timeout(2_000) });
        if (response.status === 401) throw Object.assign(new Error('Controller authentication failed.'), { code: 'CONTROLLER_AUTH_FAILED' });
        if (!response.ok || (await response.json()).accepted !== true) throw new Error('The controller did not accept authenticated shutdown.');
        controllerShutdownAccepted = true;
        result.services.controller = 'shutdown_requested';
      } catch (error) { failures.push(error); }
    }
  } else {
    result.services.controller = 'already_stopped';
    try { backendRecord = readBackendProcessRecord(dataDirectory); }
    catch (error) { failures.push(error); }
    result.services.backend = backendRecord ? 'running' : 'not_running';
  }

  // Every service is reconciled independently. A failed controller auth or a
  // crashed frontend never prevents shutdown of another positively owned child.
  if (controllerShutdownAccepted && controllerRecord) {
    try { result.services.controller = await waitOwnedRecordStopped(controllerRecord); }
    catch (error) { failures.push(error); }
  } else if (controllerRecord) {
    try { result.services.controller = await stopOwnedRecord(controllerRecord); }
    catch (error) { failures.push(error); result.services.controller = 'ownership_uncertain'; }
  } else if (controllerPresent) {
    failures.push(Object.assign(new Error('Controller responded, but no launcher-owned process identity is available to confirm its shutdown.'), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' }));
    result.services.controller = 'ownership_uncertain';
  }

  if (backendRecord) {
    try {
      if (controllerShutdownAccepted) await waitOwnedRecordStopped({ ...backendRecord, label: 'managed backend' });
      else await terminateRecoveredProcess(backendRecord);
      result.services.backend = 'stopped';
    } catch (error) { failures.push(error); result.services.backend = 'ownership_uncertain'; }
  } else if (backendReportedVerified) {
    failures.push(Object.assign(new Error('Backend was reported active but no process identity was available to verify shutdown.'), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' }));
    result.services.backend = 'ownership_uncertain';
  }

  if (frontendRecord) {
    try { result.services.frontend = await stopOwnedRecord(frontendRecord); }
    catch (error) { failures.push(error); result.services.frontend = 'ownership_uncertain'; }
  } else result.services.frontend = 'already_stopped';

  const remaining = records.filter((record) => processGroupExists(record.pid));
  if (remaining.length) failures.push(Object.assign(new Error(`Owned ${remaining.map((record) => record.label).join(', ')} process groups remain.`), { code: 'SHUTDOWN_INCOMPLETE' }));
  if (failures.length) {
    result.status = failures.some((error) => error.code === 'PROCESS_OWNERSHIP_UNCERTAIN') ? 'ownership_uncertain' : 'shutdown_incomplete';
    throw Object.assign(new Error(`SpeechCraft shutdown is incomplete: ${failures.map((error) => error.message).join('; ')}`), { code: failures[0].code || 'SHUTDOWN_INCOMPLETE' });
  }
  for (const [service, port] of [['controller', CONTROLLER_PORT], ['frontend', FRONTEND_PORT]]) {
    if (!(await isPortAvailable(port)) && ['stopped', 'already_stopped'].includes(result.services[service])) {
      result.services[service] = 'unrelated_listener_detected';
      throw Object.assign(new Error(`Owned SpeechCraft ${service} processes stopped, but an unrelated listener occupies port ${port}.`), { code: 'UNRELATED_LISTENER_DETECTED' });
    }
  }
  try { fs.unlinkSync(launcherOwnershipPath(dataDirectory)); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  result.stopped = true;
  result.status = controllerPresent ? 'shutdown_completed' : 'already_stopped';
  return result;
}

function readBackendProcessRecord(dataDirectory) {
  const filename = path.join(dataDirectory, 'state.json');
  let info;
  try { info = fs.lstatSync(filename); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  if (!info.isFile() || info.isSymbolicLink() || (typeof process.getuid === 'function' && info.uid !== process.getuid())
      || (info.mode & 0o077) !== 0 || info.size > 1_048_576 || fs.realpathSync(filename) !== filename) {
    throw Object.assign(new Error('Controller state cannot be safely inspected for a managed backend process.'), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  }
  const state = JSON.parse(fs.readFileSync(filename, 'utf8'));
  const record = state.service_process;
  if (!record) return null;
  if (record.kind !== 'speechcraft-backend' || typeof record.nonce !== 'string' || typeof record.identity !== 'string'
      || !Number.isSafeInteger(record.pid) || record.pid < 2 || record.process_group_id !== record.pid) {
    throw Object.assign(new Error('Managed backend ownership record is incomplete; no process was stopped.'), { code: 'PROCESS_OWNERSHIP_UNCERTAIN' });
  }
  return record;
}

async function waitOwnedRecordStopped(record) {
  const deadline = Date.now() + 12_000;
  while (Date.now() < deadline) {
    if (!processGroupExists(record.pid)) return 'stopped';
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw Object.assign(new Error(`Shutdown was requested, but owned ${record.label || record.kind || 'service'} process group remains.`), { code: 'SHUTDOWN_INCOMPLETE' });
}

export async function verifyFrontendOwnership(origin, identity, processHandle = null) {
  const publicResponse = await fetch(`${origin}/api/speechcraft-launcher/health`, {
    cache: 'no-store', signal: AbortSignal.timeout(1_000),
  });
  if (!publicResponse.ok) throw new Error('Frontend is not responding.');
  const publicHealth = await publicResponse.json();
  if (publicHealth.service !== 'speechcraft-frontend') throw new Error('Unexpected frontend service response.');
  const challenge = crypto.randomBytes(32).toString('base64url');
  const privateResponse = await fetch(`${origin}/api/speechcraft-launcher/verify`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ challenge }), cache: 'no-store',
    // In Next.js development mode this route can compile after the public
    // health handler has already answered. Keep the wait bounded, but don't
    // mistake that cold-route compile for an unrelated listener.
    signal: AbortSignal.timeout(5_000),
  });
  let proof = null;
  try { proof = (await privateResponse.json()).proof; } catch {}
  const expected = crypto.createHmac('sha256', Buffer.from(identity, 'base64url')).update(challenge).digest('base64url');
  if (privateResponse.status !== 200 || typeof proof !== 'string'
      || !/^[A-Za-z0-9_-]{43}$/.test(proof)
      || !crypto.timingSafeEqual(Buffer.from(proof), Buffer.from(expected))) {
    if (processHandle) throw Object.assign(new Error('The launched frontend did not prove its private installation identity.'), { code: 'FRONTEND_IDENTITY_MISMATCH' });
    throw new Error('Private frontend identity did not match.');
  }
  return true;
}

export async function waitForRestoration(session, timeoutMs = RESTORATION_TIMEOUT_MS, signal = null, pollMs = 200) {
  const deadline = Date.now() + timeoutMs;
  let latest;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw Object.assign(new Error('SpeechCraft startup was interrupted.'), { code: 'LAUNCH_CANCELLED' });
    latest = await getAuthenticatedStatus(session);
    const backend = latest.components?.backend_service;
    if (backend?.state === 'verified' && backend?.verification === 'internal_health_identity') return { status: latest, ready: true, warning: null };
    const expectedRestorePending = backend?.state === 'blocked'
      && ['service_start_pending', 'BACKEND_RESTART_PENDING'].includes(backend.failure_code);
    if (['failed', 'blocked'].includes(backend?.state) && !expectedRestorePending) return { status: latest, ready: false,
      warning: backend.message || 'Managed backend restoration is blocked; use /setup to inspect and repair this installation.' };
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }
  return { status: latest, ready: false,
    warning: 'Managed service restoration is still pending. SpeechCraft has not confirmed processing readiness; open /setup to inspect status.' };
}

export function ownedChildEnvironment(env, nonce) {
  // Keep the identity marker first. Darwin `ps eww` can truncate long
  // environment listings after a child changes its process title.
  const childEnv = { SPEECHCRAFT_OWNED_PROCESS_NONCE: nonce, ...env };
  childEnv.SPEECHCRAFT_OWNED_PROCESS_NONCE = nonce;
  return childEnv;
}

function launch(executable, args, { cwd, env, label, logSession }) {
  const nonce = crypto.randomBytes(24).toString('hex');
  const childEnv = ownedChildEnvironment(env, nonce);
  const child = spawn(executable, args, { cwd, env: childEnv, shell: false, detached: true,
    stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  child.stdout?.on('error', () => {});
  child.stderr?.on('error', () => {});
  logSession?.attach('stdout', child.stdout);
  logSession?.attach('stderr', child.stderr);
  const ready = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('spawn', resolve);
  });
  const identity = new Promise((resolve) => ready.then(() => resolve(ownedProcessIdentity(child.pid, nonce)), () => resolve(null)));
  return { child, label, nonce, ready, identity, identityValue: null, exit: new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal }))) };
}

export async function stopOwned(handle, { identityResolver = ownedProcessIdentity, signalGroup = (pid, signal) => process.kill(-pid, signal) } = {}) {
  if (!handle) return;
  if (handle.child.exitCode !== null || handle.child.signalCode !== null) {
    try {
      process.kill(-handle.child.pid, 0);
      throw new Error(`Cannot confirm the process group for ${handle.label} stopped after its leader exited; ownership is uncertain. Inspect process group ${handle.child.pid} before retrying.`);
    } catch (error) {
      if (error.code === 'ESRCH') return;
      if (!error.code) throw error;
      throw error;
    }
  }
  const ownedIdentity = await handle.identity;
  const actualIdentity = identityResolver(handle.child.pid, handle.nonce);
  // The Darwin verifier requires the launch nonce; Linux ignores it and
  // verifies boot ID, start ticks, and process group.
  if (!ownedIdentity || !actualIdentity || (ownedIdentity !== actualIdentity && !ownedIdentity.startsWith(`${actualIdentity}:`))) {
    throw new Error(`Cannot safely stop ${handle.label}; its process identity changed. Leave ownership intact and inspect the local process.`);
  }
  try { signalGroup(handle.child.pid, 'SIGTERM'); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
  const done = await bounded(handle.exit.then(() => true), STOP_TIMEOUT_MS, () => false);
  if (done) {
    if (processGroupExists(handle.child.pid)) throw new Error(`Cannot confirm the process group for ${handle.label} stopped after its leader exited; ownership is uncertain.`);
    return;
  }
  const current = identityResolver(handle.child.pid, handle.nonce);
  if (!current || (ownedIdentity !== current && !ownedIdentity.startsWith(`${current}:`))) throw new Error(`Cannot safely escalate shutdown of ${handle.label}; ownership is uncertain.`);
  try { signalGroup(handle.child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  await bounded(handle.exit, STOP_TIMEOUT_MS, () => { throw new Error(`${handle.label} did not stop; ownership remains uncertain.`); });
}

export async function openDefaultBrowser(url, { platform = process.platform, env = process.env, spawnImpl = spawn, wsl = false } = {}) {
  if (!/^http:\/\/127\.0\.0\.1:3002\/(?:setup|login)$/.test(url)) throw new Error('Refusing to open a non-SpeechCraft local URL.');
  if (platform === 'linux' && !wsl && !env.DISPLAY && !env.WAYLAND_DISPLAY) return false;
  if (platform === 'darwin' && env.SSH_CONNECTION) return false;
  let command; let args;
  if (platform === 'darwin') { command = 'open'; args = [url]; }
  else if (platform === 'linux' && wsl) { command = 'wslview'; args = [url]; }
  else if (platform === 'linux') { command = 'xdg-open'; args = [url]; }
  else throw new Error(`Default browser opening is unsupported for ${platform}.`);
  const opener = spawnImpl(command, args, { detached: true, stdio: 'ignore', shell: false, windowsHide: true });
  const started = await Promise.race([
    new Promise((resolve) => opener.once('spawn', () => resolve(true))),
    new Promise((resolve) => opener.once('error', () => resolve(false))),
  ]);
  if (!started && platform === 'linux' && wsl) {
    // The URL above is fixed to a loopback SpeechCraft route and contains no
    // user-provided data. cmd.exe still receives an argument array; no shell
    // command string is constructed by SpeechCraft.
    const windows = spawnImpl('cmd.exe', ['/c', 'start', '', url], { detached: true, stdio: 'ignore', shell: false, windowsHide: true });
    const windowsStarted = await Promise.race([
      new Promise((resolve) => windows.once('spawn', () => resolve(true))),
      new Promise((resolve) => windows.once('error', () => resolve(false))),
    ]);
    if (!windowsStarted) return false;
    windows.unref();
    return true;
  }
  if (!started) return false;
  opener.unref();
  return true;
}

export async function stopForUninstall(dataDirectory) {
  let info;
  try { info = fs.lstatSync(dataDirectory); }
  catch (error) {
    if (error.code !== 'ENOENT') {
      throw new Error(`Cannot inspect the data directory (${error.message}). Uninstall aborted; no application files were removed.`);
    }
    if (!(await isPortAvailable(CONTROLLER_PORT)) || !(await isPortAvailable(FRONTEND_PORT))) {
      throw new Error('A process is listening on SpeechCraft port 4317 or 3002, but there is no data directory credential to authenticate shutdown. Uninstall aborted; no application files were removed.');
    }
    return { stopped: true, status: 'not_running' };
  }
  if (!info.isDirectory() || info.isSymbolicLink() || (typeof process.getuid === 'function' && info.uid !== process.getuid())) {
    throw new Error('SpeechCraft data directory is not a real directory owned by the current user. Uninstall aborted; no application files were removed.');
  }
  try {
    return await stopManagedApplication(dataDirectory);
  } catch (error) {
    throw new Error(`${error.message} Uninstall aborted; no application files were removed.`);
  }
}

export async function runLauncher({ args = process.argv.slice(2), env = process.env, signal = null,
  onUrl = (url) => console.log(`SpeechCraft is available at ${url}`), open = openDefaultBrowser } = {}) {
  const options = parseArgs(args);
  if (options.help) {
    console.log([
      'Usage: speechcraft [--data-dir PATH] [--development] [--simulated] [--no-browser] [--verbose]',
      '       speechcraft status [--json] [--data-dir PATH]',
      '       speechcraft stop [--data-dir PATH]',
      '       speechcraft uninstall [--yes|--force] [--purge|--remove-data] [--data-dir PATH]',
      'SPEECHCRAFT_LOG_LEVEL=verbose prints service output. Services stay on 127.0.0.1.',
      'uninstall removes the per-user SpeechCraft application and keeps user data unless --purge is also confirmed.',
    ].join('\n'));
    return { help: true };
  }
  if (options.command === 'uninstall') {
    const wsl = process.platform === 'linux' && hostEnvironment().wsl === true;
    const uninstall = process.platform === 'darwin' ? runMacUninstall : runLinuxUninstall;
    const result = await uninstall({
      home: os.homedir(),
      env,
      yes: options.yes,
      purge: options.purge,
      dataDirectory: options.dataDirectory,
      stdin: process.stdin,
      stdout: process.stdout,
      stderr: process.stderr,
      isTTY: process.stdin.isTTY === true,
      platform: process.platform,
      cwd: process.cwd(),
      invocationRoot: ROOT,
      wsl,
      stopServices: stopForUninstall,
    });
    return { command: 'uninstall', ...result };
  }
  if (options.proveFrontend) {
    const host = hostEnvironment();
    const launchContext = resolveLaunchContext({ dataDirectory: options.dataDirectory, env, host, home: os.homedir() });
    const identity = readOrCreateFrontendIdentity(launchContext.dataDirectory);
    const proof = crypto.createHmac('sha256', Buffer.from(identity, 'base64url')).update(options.proveFrontend).digest('base64url');
    process.stdout.write(`${proof}\n`);
    return { command: 'prove-frontend' };
  }
  const host = hostEnvironment();
  if (!((host.os === 'linux' && host.architecture === 'x86_64') || (host.os === 'macos' && host.architecture === 'arm64')))
    throw new Error('SpeechCraft supports Linux x86-64, Apple Silicon macOS, and Windows through Ubuntu WSL2.');
  if (host.wsl) {
    assertCurrentWslGuest(host, {
      freeBytes: freeBytes(os.homedir()),
      requiredBytes: WSL_LAUNCH_FREE_BYTES,
      loopbackAvailable: await probeLoopback(),
      processGroupAvailable: await probeProcessGroup(),
    });
    const runtime = selectRuntimeProfile(host);
    if (runtime.profile !== 'linux-x86_64' || runtime.modelPlatform !== 'windows-wsl2') {
      throw new Error('WSL2 did not select the trusted Linux x86-64 runtime and windows-wsl2 model profile.');
    }
    validateManagedStorageTargets(host, [ROOT]);
  }
  const launchContext = resolveLaunchContext({ dataDirectory: options.dataDirectory, env, host, home: os.homedir() });
  let dataDirectory = launchContext.dataDirectory;
  const launchEnv = launchContext.env;
  if (options.command === 'status' || options.command === 'stop') {
    if (!fs.existsSync(dataDirectory)) {
      const missing = { running: false, controller: 'unavailable', frontend: 'unavailable', url: `http://${LOOPBACK}:${FRONTEND_PORT}` };
      if (options.command === 'status') printCliStatus(missing, options.json);
      else console.log('SpeechCraft is not running for this data directory.');
      return { command: options.command, status: missing };
    }
    if (options.command === 'status') {
      const value = await controllerCliStatus(dataDirectory);
      printCliStatus(value, options.json);
      return { command: 'status', status: value };
    }
    const result = await stopManagedApplication(dataDirectory);
    console.log(result.stopped ? 'SpeechCraft stopped.' : 'SpeechCraft shutdown was not confirmed.');
    console.log(`Shutdown status: ${result.status}.`);
    for (const [service, state] of Object.entries(result.services)) console.log(`${service}: ${state}`);
    return { command: 'stop', ...result };
  }
  assertLoopbackAddress(LOOPBACK);
  const preparedFrontend = verifySourceCheckout(options.development);
  fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
  const dirInfo = fs.lstatSync(dataDirectory);
  if (!dirInfo.isDirectory() || dirInfo.isSymbolicLink() || (typeof process.getuid === 'function' && dirInfo.uid !== process.getuid())) {
    throw new Error('SpeechCraft data directory must be a real directory owned by the current user.');
  }
  fs.chmodSync(dataDirectory, 0o700);
  dataDirectory = fs.realpathSync(dataDirectory);
  validateManagedStorage(host, managedStorageLocations(dataDirectory));
  assertExistingManagedFiles(host, [
    path.join(dataDirectory, 'bootstrap.token'),
    path.join(dataDirectory, 'launcher.processes.json'),
  ]);
  const installId = crypto.createHash('sha256').update(`${dataDirectory}\0${host.os}\0${host.architecture}`).digest('hex');
  const frontendIdentity = readOrCreateFrontendIdentity(dataDirectory);
  const appOrigin = `http://${LOOPBACK}:${FRONTEND_PORT}`;
  const controllerOrigin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const common = { ...launchEnv, SPEECHCRAFT_SETUP_DATA_DIR: dataDirectory,
    SPEECHCRAFT_SETUP_PORT: String(CONTROLLER_PORT), SPEECHCRAFT_SETUP_MODE: options.simulated ? 'simulated' : 'runtime',
    SPEECHCRAFT_SETUP_ALLOWED_HOSTS: `${LOOPBACK}:${CONTROLLER_PORT},localhost:${CONTROLLER_PORT},${LOOPBACK}:${FRONTEND_PORT},localhost:${FRONTEND_PORT}`,
    SPEECHCRAFT_SETUP_ALLOWED_ORIGINS: `${controllerOrigin},http://localhost:${CONTROLLER_PORT},${appOrigin},http://localhost:${FRONTEND_PORT}`,
    SPEECHCRAFT_APPLICATION_ROOT: ROOT, SPEECHCRAFT_BACKEND_PORT: String(BACKEND_PORT),
    SPEECHCRAFT_BACKEND_URL: `http://${LOOPBACK}:${BACKEND_PORT}` };
  if (host.wsl) {
    const managedTmp = path.join(dataDirectory, 'cache', 'bootstrap', 'tmp');
    const huggingFace = path.join(dataDirectory, 'cache', 'bootstrap', 'huggingface');
    Object.assign(common, {
      HF_HOME: huggingFace,
      HUGGINGFACE_HUB_CACHE: path.join(huggingFace, 'hub'),
      HF_XET_CACHE: path.join(huggingFace, 'xet'),
      TMPDIR: managedTmp,
      TEMP: managedTmp,
      TMP: managedTmp,
      UV_CACHE_DIR: path.join(dataDirectory, 'cache', 'bootstrap', 'uv'),
      XDG_CACHE_HOME: path.join(dataDirectory, 'cache', 'bootstrap', 'home', '.cache'),
      XDG_DATA_HOME: path.join(os.homedir(), '.local', 'share'),
    });
  }
  const releaseManifestPath = path.join(ROOT, 'application-release-manifest.json');
  if (fs.existsSync(releaseManifestPath)) {
    const release = JSON.parse(fs.readFileSync(releaseManifestPath, 'utf8'));
    const wheel = release?.trusted_inputs?.model_manager_wheel;
    if (!wheel || typeof wheel.path !== 'string' || !/^[a-f0-9]{64}$/.test(wheel.sha256 || '')) throw new Error('Packaged model-manager trust metadata is incomplete.');
    const wheelPath = path.resolve(ROOT, wheel.path);
    if (!wheelPath.startsWith(`${ROOT}${path.sep}`) || !fs.statSync(wheelPath).isFile()) throw new Error('Packaged model-manager wheel is missing or outside the application resources.');
    const digest = crypto.createHash('sha256').update(fs.readFileSync(wheelPath)).digest('hex');
    if (digest !== wheel.sha256) throw new Error('Packaged model-manager wheel does not match its release identity.');
    common.SPEECHCRAFT_MODEL_MANAGER_WHEEL = wheelPath;
    common.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256 = wheel.sha256;
  }
  const children = [];
  let shuttingDown = false;
  let shutdownPromise;
  let serviceLog = null;
  const shutdown = () => {
    if (shutdownPromise) return shutdownPromise;
    shuttingDown = true;
    shutdownPromise = (async () => {
      for (const child of [...children].reverse()) await stopOwned(child);
      // Drop only records whose process groups have actually ended. A
      // controller reused from a prior launcher must remain owned after this
      // frontend-only launch exits.
      persistLauncherOwnership(dataDirectory, []);
      try { serviceLog?.close(); } catch { /* a log-file failure must not change shutdown */ }
    })();
    return shutdownPromise;
  };

  try {
  const outputMode = launchOutputMode({ verbose: options.verbose, env: launchEnv, stdoutIsTTY: process.stdout.isTTY === true });
  serviceLog = createLaunchLogSession(dataDirectory, { verbose: outputMode.verbose });
  console.log(`SpeechCraft: starting on ${host.os} ${host.architecture}; services stay on ${LOOPBACK}`);
  console.log(serviceLog.fileEnabled
    ? `SpeechCraft: detailed log: ${serviceLog.path}`
    : 'SpeechCraft: detailed log unavailable; startup is continuing');
  let controllerHandle = null;
  if (await isPortAvailable(CONTROLLER_PORT)) {
    console.log(`SpeechCraft: starting the setup controller at http://${LOOPBACK}:${CONTROLLER_PORT}`);
    const controllerEntry = path.join(ROOT, 'setup-controller/src/server.js');
    controllerHandle = launch(process.execPath, [controllerEntry, '--data-dir', dataDirectory,
      '--port', String(CONTROLLER_PORT), '--mode', options.simulated ? 'simulated' : 'runtime'],
    { cwd: ROOT, env: common, label: 'setup controller', logSession: serviceLog });
    children.push(controllerHandle);
    await controllerHandle.ready;
    controllerHandle.identityValue = await controllerHandle.identity;
    if (!controllerHandle.identityValue) throw new Error('Could not establish setup controller process identity.');
    persistLauncherOwnership(dataDirectory, children);
    await waitFor(`${controllerOrigin}/setup-api/health`, async (response) => response.ok && (await response.json()).status === 'ok', STARTUP_TIMEOUT_MS, signal, controllerHandle);
    console.log('SpeechCraft: setup controller is healthy');
  } else {
    console.log(`SpeechCraft: using the setup controller already listening at http://${LOOPBACK}:${CONTROLLER_PORT}`);
    // Reuse only a controller that proves possession of this data directory's
    // private bootstrap capability; never infer ownership from its PID/port.
    try { await fetch(`${controllerOrigin}/setup-api/health`, { signal: AbortSignal.timeout(1_000) }).then((r) => { if (!r.ok) throw new Error(); }); }
    catch { throw new Error('Setup controller port 4317 is occupied by an unrelated listener; SpeechCraft will not stop it.'); }
  }
  const controllerSession = await getControllerSession(dataDirectory);
  const status = controllerSession.status;
  const expectedMode = options.simulated ? 'simulated' : 'runtime';
  if (status.mode !== expectedMode) throw new Error(`The running controller is in ${status.mode} mode, but this launch requested ${expectedMode}; stop it safely before retrying.`);

  const frontendHealth = `${appOrigin}/api/speechcraft-launcher/health`;
  let frontendHandle = null;
  if (await isPortAvailable(FRONTEND_PORT)) {
    console.log(`SpeechCraft: starting the frontend at http://${LOOPBACK}:${FRONTEND_PORT}`);
    const frontendArgs = preparedFrontend.command === 'standalone'
      ? [preparedFrontend.nextEntry]
      : [preparedFrontend.nextEntry, preparedFrontend.command, '--hostname', LOOPBACK, '-p', String(FRONTEND_PORT)];
    frontendHandle = launch(process.execPath, frontendArgs, { cwd: preparedFrontend.cwd, label: 'Next.js frontend',
      env: { ...common, HOSTNAME: LOOPBACK, PORT: String(FRONTEND_PORT), NODE_ENV: options.development ? 'development' : 'production',
        SPEECHCRAFT_SETUP_SESSION_BRIDGE: '1', SPEECHCRAFT_SETUP_BRIDGE_LOOPBACK_CONFIRMED: '1',
        SPEECHCRAFT_SETUP_CONTROLLER_URL: controllerOrigin,
        SPEECHCRAFT_SETUP_BOOTSTRAP_FILE: path.join(dataDirectory, 'bootstrap.token'),
        SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS: `${appOrigin},http://localhost:${FRONTEND_PORT}`,
        SPEECHCRAFT_LAUNCHER_INSTALL_ID: installId,
        SPEECHCRAFT_LAUNCHER_PRIVATE_IDENTITY: frontendIdentity }, logSession: serviceLog });
    children.push(frontendHandle);
    await frontendHandle.ready;
    frontendHandle.identityValue = await frontendHandle.identity;
    if (!frontendHandle.identityValue) throw new Error('Could not establish frontend process identity.');
    persistLauncherOwnership(dataDirectory, children);
    await waitFor(frontendHealth, async (response) => response.ok, STARTUP_TIMEOUT_MS, signal, frontendHandle);
    await verifyFrontendOwnership(appOrigin, frontendIdentity, frontendHandle);
    console.log('SpeechCraft: frontend is healthy');
  } else {
    console.log(`SpeechCraft: checking the frontend already listening at http://${LOOPBACK}:${FRONTEND_PORT}`);
    try { await verifyFrontendOwnership(appOrigin, frontendIdentity, null); }
    catch { throw new Error('Frontend port 3002 is occupied by a different or unverified application; SpeechCraft will not stop it.'); }
    console.log('SpeechCraft: frontend identity matches this installation');
  }
  const route = chooseDestination(status.onboarding);
  console.log(route === '/setup'
    ? 'SpeechCraft: initial setup is incomplete'
    : 'SpeechCraft: initial setup is already complete');
  // An already onboarded installation gets an opportunity to restore its
  // managed backend. If it remains unhealthy, the normal UI still opens and
  // its repair notice points to /setup; the launcher never starts a download.
  if (status.onboarding?.completed) {
    // A user-invoked launch is an explicit restoration retry. RuntimeTasks
    // keeps concurrent requests single-flight and reuses a healthy service.
    if (status.components?.backend_service?.state !== 'verified') {
      console.log('SpeechCraft: revalidating managed models and restoring the local backend');
    }
    await requestServiceRestore(controllerSession, { retry: true });
    const restoration = await waitForRestoration(controllerSession, RESTORATION_TIMEOUT_MS, signal);
    controllerSession.status = restoration.status || status;
    if (!restoration.ready) console.error(`SpeechCraft repair required: ${restoration.warning}`);
  }
  const url = `${appOrigin}${route}`;
  if (outputMode.emitReadyEvent) console.log(formatFrontendReadyEvent({ url, route, ready: true, owned: children.length > 0 }));
  onUrl(url);
  let opened = false;
  if (options.openBrowser) {
    try { opened = await open(url, { wsl: host.wsl, platform: process.platform, env }); }
    catch { opened = false; }
  }
  console.log(formatLaunchAccess({ url, opened, env, browserRequested: options.openBrowser }));
  const unexpected = Promise.race(children.map((child) => child.exit.then((result) => ({ child, result }))))
    .then(async (event) => {
      if (event.child.label === 'setup controller' && event.result.code === 0 && event.result.signal === null) {
        return { ...event, intentional: true };
      }
      if (event.child.label === 'Next.js frontend') {
        // `speechcraft stop` can be run by a separate CLI after this launcher
        // reused a controller left by an earlier launcher. The stop command
        // closes that authenticated controller before terminating this
        // frontend. Recognize that ordered shutdown without treating a port
        // response as process ownership; a still-healthy controller means the
        // frontend exit is unexpected.
        try {
          const response = await fetch(`${controllerOrigin}/setup-api/health`, {
            cache: 'no-store', signal: AbortSignal.timeout(700),
          });
          if (!response.ok) return { ...event, intentional: true };
        } catch { return { ...event, intentional: true }; }
      }
      return event;
    });
  return { url, status: controllerSession.status, children, shutdown, unexpected, get shuttingDown() { return shuttingDown; },
    ownedChildren: children.length, reusedController: !controllerHandle, reusedFrontend: !frontendHandle };
  } catch (error) {
    try { await shutdown(); } catch (cleanupError) {
      throw new Error(`${error.message} Cleanup could not confirm all owned children stopped: ${cleanupError.message}`, { cause: error });
    }
    throw error;
  }
}
