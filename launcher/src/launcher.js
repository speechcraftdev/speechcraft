import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { defaultDataDirectory } from '../../setup-controller/src/paths.js';
import { ownedProcessIdentity } from '../../setup-controller/src/bootstrap.js';
import { hostEnvironment, validateManagedStorageTargets } from '../../setup-controller/src/platform.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const FRONTEND_PORT = 3002;
const CONTROLLER_PORT = 4317;
const BACKEND_PORT = 8010;
const LOOPBACK = '127.0.0.1';
const STARTUP_TIMEOUT_MS = 45_000;
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
  const result = { dataDirectory: null, development: false, simulated: false, openBrowser: true };
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === '--data-dir' && args[index + 1]) result.dataDirectory = path.resolve(args[++index]);
    else if (arg === '--development') result.development = true;
    else if (arg === '--simulated') result.simulated = true;
    else if (arg === '--no-browser') result.openBrowser = false;
    else if (arg === '--help' || arg === '-h') result.help = true;
    else throw new Error(`Unknown launcher option: ${arg}`);
  }
  return result;
}

export function chooseDestination(onboarding) {
  return onboarding?.completed === true ? '/login' : '/setup';
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
  return { nextEntry, command: development ? 'dev' : 'start' };
}

function isPortAvailable(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', (error) => error.code === 'EADDRINUSE' ? resolve(false) : reject(error));
    server.listen(port, LOOPBACK, () => server.close((error) => error ? reject(error) : resolve(true)));
  });
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

async function getControllerStatus(dataDirectory) {
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
    const response = await fetch(`${origin}/setup-api/status`, { headers: { host: `${LOOPBACK}:${CONTROLLER_PORT}`, cookie }, cache: 'no-store' });
    if (!response.ok) throw new Error('The local controller status could not be read.');
    return response.json();
  } finally {
    // JavaScript strings cannot be reliably zeroed; keep the secret short-lived
    // and never emit it to logs, arguments, or persistent launcher state.
  }
}

function launch(executable, args, { cwd, env, label }) {
  const nonce = crypto.randomBytes(24).toString('hex');
  const childEnv = { ...env, SPEECHCRAFT_OWNED_PROCESS_NONCE: nonce };
  const child = spawn(executable, args, { cwd, env: childEnv, shell: false, detached: true,
    stdio: ['ignore', 'inherit', 'inherit'], windowsHide: true });
  const ready = new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('spawn', resolve);
  });
  const identity = new Promise((resolve) => ready.then(() => resolve(ownedProcessIdentity(child.pid, nonce)), () => resolve(null)));
  return { child, label, ready, identity, exit: new Promise((resolve) => child.once('exit', (code, signal) => resolve({ code, signal }))) };
}

async function stopOwned(handle) {
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
  const actualIdentity = ownedProcessIdentity(handle.child.pid);
  // Linux start-time and process-group identity is verified before signalling.
  if (!ownedIdentity || !actualIdentity || (ownedIdentity !== actualIdentity && !ownedIdentity.startsWith(`${actualIdentity}:`))) {
    throw new Error(`Cannot safely stop ${handle.label}; its process identity changed. Leave ownership intact and inspect the local process.`);
  }
  try { process.kill(-handle.child.pid, 'SIGTERM'); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
  const done = await bounded(handle.exit.then(() => true), STOP_TIMEOUT_MS, () => false);
  if (done) return;
  const current = ownedProcessIdentity(handle.child.pid);
  if (!current || !ownedIdentity.startsWith(`${current}:`)) throw new Error(`Cannot safely escalate shutdown of ${handle.label}; ownership is uncertain.`);
  try { process.kill(-handle.child.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  await bounded(handle.exit, STOP_TIMEOUT_MS, () => { throw new Error(`${handle.label} did not stop; ownership remains uncertain.`); });
}

async function openBrowser(url) {
  if (!process.env.DISPLAY && !process.env.WAYLAND_DISPLAY) return false;
  const opener = spawn('xdg-open', [url], { detached: true, stdio: 'ignore', shell: false });
  const started = await Promise.race([
    new Promise((resolve) => opener.once('spawn', () => resolve(true))),
    new Promise((resolve) => opener.once('error', () => resolve(false))),
  ]);
  if (!started) return false;
  opener.unref();
  return true;
}

export async function runLauncher({ args = process.argv.slice(2), env = process.env, signal = null,
  onUrl = (url) => console.log(`SpeechCraft is available at ${url}`), open = openBrowser } = {}) {
  const options = parseArgs(args);
  if (options.help) {
    console.log('Usage: speechcraft [--data-dir PATH] [--development] [--simulated] [--no-browser]');
    return { help: true };
  }
  if (process.platform !== 'linux' || process.arch !== 'x64') throw new Error('This source-checkout launcher supports Linux x86-64. Use Ubuntu WSL2 on Windows; native Windows and macOS launchers are not included.');
  assertLoopbackAddress(LOOPBACK);
  const preparedFrontend = verifySourceCheckout(options.development);
  let dataDirectory = path.resolve(options.dataDirectory || env.SPEECHCRAFT_SETUP_DATA_DIR || defaultDataDirectory());
  fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
  const dirInfo = fs.lstatSync(dataDirectory);
  if (!dirInfo.isDirectory() || dirInfo.isSymbolicLink() || (typeof process.getuid === 'function' && dirInfo.uid !== process.getuid())) {
    throw new Error('SpeechCraft data directory must be a real directory owned by the current user.');
  }
  fs.chmodSync(dataDirectory, 0o700);
  dataDirectory = fs.realpathSync(dataDirectory);
  validateManagedStorageTargets(hostEnvironment(), [dataDirectory]);
  const installId = crypto.createHash('sha256').update(`${dataDirectory}\0linux\0x64`).digest('hex');
  const appOrigin = `http://${LOOPBACK}:${FRONTEND_PORT}`;
  const controllerOrigin = `http://${LOOPBACK}:${CONTROLLER_PORT}`;
  const common = { ...env, SPEECHCRAFT_SETUP_DATA_DIR: dataDirectory,
    SPEECHCRAFT_SETUP_PORT: String(CONTROLLER_PORT), SPEECHCRAFT_SETUP_MODE: options.simulated ? 'simulated' : 'runtime',
    SPEECHCRAFT_SETUP_ALLOWED_HOSTS: `${LOOPBACK}:${CONTROLLER_PORT},localhost:${CONTROLLER_PORT},${LOOPBACK}:${FRONTEND_PORT},localhost:${FRONTEND_PORT}`,
    SPEECHCRAFT_SETUP_ALLOWED_ORIGINS: `${controllerOrigin},http://localhost:${CONTROLLER_PORT},${appOrigin},http://localhost:${FRONTEND_PORT}`,
    SPEECHCRAFT_APPLICATION_ROOT: ROOT, SPEECHCRAFT_BACKEND_PORT: String(BACKEND_PORT),
    SPEECHCRAFT_BACKEND_URL: `http://${LOOPBACK}:${BACKEND_PORT}` };
  const children = [];
  let shuttingDown = false;
  let shutdownPromise;
  const shutdown = () => {
    if (shutdownPromise) return shutdownPromise;
    shuttingDown = true;
    shutdownPromise = (async () => {
      for (const child of [...children].reverse()) await stopOwned(child);
    })();
    return shutdownPromise;
  };

  try {
  let controllerHandle = null;
  if (await isPortAvailable(CONTROLLER_PORT)) {
    const controllerEntry = path.join(ROOT, 'setup-controller/src/server.js');
    controllerHandle = launch(process.execPath, [controllerEntry, '--data-dir', dataDirectory,
      '--port', String(CONTROLLER_PORT), '--mode', options.simulated ? 'simulated' : 'runtime'],
    { cwd: ROOT, env: common, label: 'setup controller' });
    children.push(controllerHandle);
    await controllerHandle.ready;
    await waitFor(`${controllerOrigin}/setup-api/health`, async (response) => response.ok && (await response.json()).status === 'ok', STARTUP_TIMEOUT_MS, signal, controllerHandle);
  } else {
    // Reuse only a controller that proves possession of this data directory's
    // private bootstrap capability; never infer ownership from its PID/port.
    try { await fetch(`${controllerOrigin}/setup-api/health`, { signal: AbortSignal.timeout(1_000) }).then((r) => { if (!r.ok) throw new Error(); }); }
    catch { throw new Error('Setup controller port 4317 is occupied by an unrelated listener; SpeechCraft will not stop it.'); }
  }
  const status = await getControllerStatus(dataDirectory);
  const expectedMode = options.simulated ? 'simulated' : 'runtime';
  if (status.mode !== expectedMode) throw new Error(`The running controller is in ${status.mode} mode, but this launch requested ${expectedMode}; stop it safely before retrying.`);

  const frontendHealth = `${appOrigin}/api/speechcraft-launcher/health`;
  let frontendHandle = null;
  if (await isPortAvailable(FRONTEND_PORT)) {
    frontendHandle = launch(process.execPath, [preparedFrontend.nextEntry, preparedFrontend.command,
      '--hostname', LOOPBACK, '-p', String(FRONTEND_PORT)], { cwd: path.join(ROOT, 'frontend'), label: 'Next.js frontend',
      env: { ...common, NODE_ENV: options.development ? 'development' : 'production',
        SPEECHCRAFT_SETUP_SESSION_BRIDGE: '1', SPEECHCRAFT_SETUP_BRIDGE_LOOPBACK_CONFIRMED: '1',
        SPEECHCRAFT_SETUP_CONTROLLER_URL: controllerOrigin,
        SPEECHCRAFT_SETUP_BOOTSTRAP_FILE: path.join(dataDirectory, 'bootstrap.token'),
        SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS: `${appOrigin},http://localhost:${FRONTEND_PORT}`,
        SPEECHCRAFT_LAUNCHER_INSTALL_ID: installId } });
    children.push(frontendHandle);
    await frontendHandle.ready;
    await waitFor(frontendHealth, async (response) => {
      if (!response.ok) return false;
      const value = await response.json();
      return value.service === 'speechcraft-frontend' && value.installation_id === installId;
    }, STARTUP_TIMEOUT_MS, signal, frontendHandle);
  } else {
    try {
      const response = await fetch(frontendHealth, { signal: AbortSignal.timeout(1_000) });
      const value = await response.json();
      if (!response.ok || value.service !== 'speechcraft-frontend' || value.installation_id !== installId) throw new Error();
    } catch { throw new Error('Frontend port 3002 is occupied by a different or unverified application; SpeechCraft will not stop it.'); }
  }
  const route = chooseDestination(status.onboarding);
  // An already onboarded installation gets an opportunity to restore its
  // managed backend. If it remains unhealthy, the normal UI still opens and
  // its repair notice points to /setup; the launcher never starts a download.
  if (status.onboarding?.completed) {
    try {
      await waitFor(`${controllerOrigin}/setup-api/status`, async (response) => {
        if (!response.ok) return false;
        const latest = await response.json();
        return latest.components?.backend_service?.verification === 'internal_health_identity'
          || latest.components?.backend_service?.state === 'failed';
      }, 20_000, signal);
    } catch { /* Show the local UI and its current repair state. */ }
  }
  const url = `${appOrigin}${route}`;
  onUrl(url);
  if (options.openBrowser) {
    try { if (!await open(url)) console.log(`Headless Linux detected. Open ${url} through an SSH tunnel, for example: ssh -N -L 3002:127.0.0.1:3002 user@cozytent-host`); }
    catch { console.log(`Could not open a browser. Open ${url} manually.`); }
  }
  const unexpected = Promise.race(children.map((child) => child.exit.then((result) => ({ child, result }))));
  return { url, status, children, shutdown, unexpected, get shuttingDown() { return shuttingDown; },
    ownedChildren: children.length, reusedController: !controllerHandle, reusedFrontend: !frontendHandle };
  } catch (error) {
    try { await shutdown(); } catch (cleanupError) {
      throw new Error(`${error.message} Cleanup could not confirm all owned children stopped: ${cleanupError.message}`, { cause: error });
    }
    throw error;
  }
}
