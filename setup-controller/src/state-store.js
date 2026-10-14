import { execFileSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const STATE_VERSION = 1;
export const EVENT_LIMIT = 256;
const OWNER_VERSION = 1;
const OWNER_GRACE_MS = 5_000;

export class StateStoreError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'StateStoreError';
    this.code = code;
  }
}

function syncDirectory(directory) {
  const fd = fs.openSync(directory, 'r');
  try {
    fs.fsyncSync(fd);
  } catch (error) {
    // macOS filesystems may reject fsync on directory descriptors. Rename
    // remains atomic, but that filesystem cannot promise rename durability.
    if (process.platform !== 'darwin' || !['EINVAL', 'ENOTSUP', 'EISDIR'].includes(error.code)) throw error;
  } finally {
    fs.closeSync(fd);
  }
}

function processIdentity(pid) {
  if (process.platform === 'linux') {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    const end = stat.lastIndexOf(')');
    if (end < 0) throw new Error('invalid proc stat');
    const fields = stat.slice(end + 2).trim().split(/\s+/);
    const startTicks = fields[19]; // proc field 22, after pid and comm.
    const bootId = fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
    if (!startTicks || !bootId) throw new Error('process identity unavailable');
    return `linux:${bootId}:${startTicks}`;
  }
  if (process.platform === 'darwin') {
    const started = execFileSync('/bin/ps', ['-o', 'lstart=', '-p', String(pid)], {
      encoding: 'utf8', timeout: 2_000, stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (!started) throw new Error('process identity unavailable');
    return `darwin:${started}`;
  }
  throw new Error(`unsupported controller platform: ${process.platform}`);
}

function safeRegularFile(file) {
  try {
    const info = fs.lstatSync(file);
    return info.isFile() && !info.isSymbolicLink();
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

export function atomicWriteFile(directory, filename, contents, mode = 0o600) {
  const target = path.join(directory, filename);
  const temporary = path.join(directory, `.${filename}.${randomBytes(12).toString('hex')}.tmp`);
  let fd;
  try {
    fd = fs.openSync(temporary, 'wx', mode);
    fs.writeFileSync(fd, contents, 'utf8');
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, target);
    syncDirectory(directory);
  } catch (error) {
    if (fd !== undefined) fs.closeSync(fd);
    try { fs.unlinkSync(temporary); } catch {}
    throw error;
  }
}

function inspectOwner(directory) {
  const directoryInfo = fs.lstatSync(directory);
  if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink()) {
    throw new StateStoreError('LOCK_UNSAFE', 'controller lock path is not a real directory');
  }
  const ownerPath = path.join(directory, 'owner.json');
  if (!safeRegularFile(ownerPath)) {
    if (Date.now() - directoryInfo.mtimeMs > OWNER_GRACE_MS) return { stale: true, ownerless: true };
    return { stale: false, reason: 'controller ownership is being established' };
  }
  if (fs.statSync(ownerPath).size > 4_096) {
    return { stale: false, reason: 'controller lock metadata is oversized; manual inspection is required' };
  }
  let owner;
  try { owner = JSON.parse(fs.readFileSync(ownerPath, 'utf8')); }
  catch { return { stale: false, reason: 'controller lock metadata is corrupt; manual inspection is required' }; }
  if (owner?.version !== OWNER_VERSION || !Number.isSafeInteger(owner.pid)
      || typeof owner.identity !== 'string' || typeof owner.nonce !== 'string') {
    return { stale: false, reason: 'controller lock metadata is invalid; manual inspection is required' };
  }
  try {
    process.kill(owner.pid, 0);
  } catch (error) {
    if (error.code === 'ESRCH') return { stale: true, ownerless: false };
    return { stale: false, reason: 'cannot establish whether the controller lock owner is alive' };
  }
  try {
    const liveIdentity = processIdentity(owner.pid);
    if (liveIdentity !== owner.identity) return { stale: true, ownerless: false };
    return { stale: false, reason: 'another controller owns this SpeechCraft installation' };
  } catch {
    // A live PID whose start identity cannot be checked is never evicted.
    return { stale: false, reason: 'cannot safely identify the controller lock owner' };
  }
}

function removeStaleLock(lockDirectory, staleName) {
  const staleInfo = fs.lstatSync(staleName);
  if (!staleInfo.isDirectory() || staleInfo.isSymbolicLink()) {
    throw new StateStoreError('LOCK_UNSAFE', 'stale controller lock path is unsafe');
  }
  const names = fs.readdirSync(staleName);
  if (names.some((name) => name !== 'owner.json')) {
    throw new StateStoreError('LOCK_UNSAFE', 'stale controller lock contains unexpected files; inspect it manually');
  }
  if (names.includes('owner.json')) {
    if (!safeRegularFile(path.join(staleName, 'owner.json'))) {
      throw new StateStoreError('LOCK_UNSAFE', 'stale controller lock has unsafe owner metadata');
    }
    fs.unlinkSync(path.join(staleName, 'owner.json'));
  }
  fs.rmdirSync(staleName);
  syncDirectory(lockDirectory);
}

function acquireOwnership(directory) {
  const lockDirectory = path.join(directory, '.controller.lock');
  const owner = {
    version: OWNER_VERSION,
    pid: process.pid,
    identity: processIdentity(process.pid),
    nonce: randomBytes(24).toString('hex'),
    createdAt: new Date().toISOString(),
  };
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      fs.mkdirSync(lockDirectory, { mode: 0o700 });
      try {
        fs.writeFileSync(path.join(lockDirectory, 'owner.json'), `${JSON.stringify(owner)}\n`, { flag: 'wx', mode: 0o600 });
        const ownerFd = fs.openSync(path.join(lockDirectory, 'owner.json'), 'r');
        try { fs.fsyncSync(ownerFd); } finally { fs.closeSync(ownerFd); }
        syncDirectory(lockDirectory);
        return { lockDirectory, owner };
      } catch (error) {
        throw new StateStoreError('LOCK_CREATE_FAILED', `could not record controller ownership: ${error.message}`);
      }
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const status = inspectOwner(lockDirectory);
      if (!status.stale) throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', status.reason);
      const staleName = path.join(directory, `.controller-stale-${randomBytes(12).toString('hex')}`);
      try {
        fs.renameSync(lockDirectory, staleName);
      } catch (renameError) {
        if (renameError.code === 'ENOENT' || renameError.code === 'EEXIST') continue;
        throw new StateStoreError('LOCK_RECOVERY_FAILED', `could not safely recover stale controller ownership: ${renameError.message}`);
      }
      removeStaleLock(directory, staleName);
    }
  }
  throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', 'another controller is acquiring ownership; retry shortly');
}

const JOB_STATES = new Set(['running', 'waiting_for_user', 'failed', 'cancelled', 'interrupted', 'completed']);

function validateState(state) {
  if (!state || state.schema_version !== STATE_VERSION || !Number.isSafeInteger(state.revision)
      || state.revision < 0 || !Array.isArray(state.events) || state.events.length > EVENT_LIMIT
      || !(state.job === null || (typeof state.job === 'object' && !Array.isArray(state.job)))
      || (state.revision === 0 && state.events.length !== 0)
      || (state.revision > 0 && (!state.events.length || state.events.at(-1)?.id !== state.revision))) {
    throw new StateStoreError('STATE_INVALID', 'setup state has an unsupported or invalid schema');
  }
  if (state.job !== null) {
    const job = state.job;
    if (job.schema_version !== 1 || typeof job.id !== 'string' || !JOB_STATES.has(job.state)
        || !Number.isSafeInteger(job.attempt_count) || job.attempt_count < 1
        || !Number.isSafeInteger(job.revision) || job.revision !== state.revision
        || typeof job.created_at !== 'string' || typeof job.updated_at !== 'string'
        || (job.stage !== null && typeof job.stage !== 'string')
        || (job.task_id !== null && typeof job.task_id !== 'string')
        || (job.error !== null && (typeof job.error.code !== 'string' || typeof job.error.message !== 'string'))) {
      throw new StateStoreError('STATE_INVALID', 'setup state job record is malformed');
    }
  }
  let lastRevision = 0;
  for (const [index, event] of state.events.entries()) {
    if (!event || !Number.isSafeInteger(event.id) || event.id <= lastRevision
        || (index > 0 && event.id !== lastRevision + 1)
        || event.id > state.revision || typeof event.type !== 'string'
        || typeof event.at !== 'string' || !event.data || typeof event.data !== 'object') {
      throw new StateStoreError('STATE_INVALID', 'setup event journal is malformed');
    }
    lastRevision = event.id;
  }
}

export class StateStore {
  constructor(directory, lock) {
    this.directory = directory;
    this.lock = lock;
    this.file = path.join(directory, 'state.json');
    this.listeners = new Set();
    this.state = null;
  }

  static open(directory) {
    const absolute = path.resolve(directory);
    fs.mkdirSync(absolute, { recursive: true, mode: 0o700 });
    const info = fs.lstatSync(absolute);
    if (!info.isDirectory() || info.isSymbolicLink()) {
      throw new StateStoreError('STATE_DIRECTORY_UNSAFE', 'setup data directory must be a real directory');
    }
    if (typeof process.getuid === 'function' && info.uid !== process.getuid()) {
      throw new StateStoreError('STATE_DIRECTORY_UNSAFE', 'setup data directory must be owned by the current user');
    }
    fs.chmodSync(absolute, 0o700);
    const lock = acquireOwnership(absolute);
    const store = new StateStore(absolute, lock);
    try {
      store.state = store.read();
      return store;
    } catch (error) {
      store.releaseOwnership();
      throw error;
    }
  }

  read() {
    if (!safeRegularFile(this.file)) {
      if (fs.existsSync(this.file)) throw new StateStoreError('STATE_UNSAFE', 'setup state must be a regular non-symlink file');
      return { schema_version: STATE_VERSION, revision: 0, job: null, events: [] };
    }
    const fileInfo = fs.lstatSync(this.file);
    if ((typeof process.getuid === 'function' && fileInfo.uid !== process.getuid())
        || (fileInfo.mode & 0o077) !== 0 || fileInfo.size > 1_048_576) {
      throw new StateStoreError('STATE_UNSAFE', 'setup state file ownership, permissions, or size are unsafe');
    }
    let state;
    try { state = JSON.parse(fs.readFileSync(this.file, 'utf8')); }
    catch { throw new StateStoreError('STATE_CORRUPT', 'setup state JSON is corrupt; preserve the file and inspect it before recovery'); }
    validateState(state);
    return state;
  }

  snapshot() { return structuredClone(this.state); }

  commit(mutator, type) {
    const next = structuredClone(this.state);
    mutator(next);
    next.revision += 1;
    const now = new Date().toISOString();
    if (next.job) {
      next.job.updated_at = now;
      next.job.revision = next.revision;
    }
    next.events.push({ id: next.revision, type, at: now, data: { job: structuredClone(next.job) } });
    if (next.events.length > EVENT_LIMIT) next.events.splice(0, next.events.length - EVENT_LIMIT);
    validateState(next);
    atomicWriteFile(this.directory, 'state.json', `${JSON.stringify(next, null, 2)}\n`);
    this.state = next;
    for (const listener of this.listeners) {
      try { listener(structuredClone(next.events.at(-1)), this.snapshot()); } catch {}
    }
    return this.snapshot();
  }

  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  releaseOwnership() {
    const { lockDirectory, owner } = this.lock;
    try {
      const current = JSON.parse(fs.readFileSync(path.join(lockDirectory, 'owner.json'), 'utf8'));
      if (current.nonce !== owner.nonce) return;
      fs.unlinkSync(path.join(lockDirectory, 'owner.json'));
      fs.rmdirSync(lockDirectory);
      syncDirectory(this.directory);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
}
