import { execFileSync } from 'node:child_process';
import { randomBytes, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const STATE_VERSION = 2;
export const EVENT_LIMIT = 256;
const OWNER_VERSION = 1;

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

function inspectOwner(lockPath) {
  const info = fs.lstatSync(lockPath);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new StateStoreError('LOCK_UNSAFE', 'controller lock path is not a regular non-symlink file');
  }
  if ((typeof process.getuid === 'function' && info.uid !== process.getuid()) || (info.mode & 0o077) !== 0) {
    throw new StateStoreError('LOCK_UNSAFE', 'controller lock ownership or permissions are unsafe; inspect it manually');
  }
  if (info.size > 4_096) {
    return { stale: false, reason: 'controller lock metadata is oversized; manual inspection is required', info };
  }
  let owner;
  try { owner = JSON.parse(fs.readFileSync(lockPath, 'utf8')); }
  catch { return { stale: false, reason: 'controller lock metadata is missing or corrupt; manual inspection is required', info }; }
  if (owner?.version !== OWNER_VERSION || !Number.isSafeInteger(owner.pid) || owner.pid < 1
      || typeof owner.identity !== 'string' || !/^[a-f0-9]{48}$/.test(owner.nonce || '')) {
    return { stale: false, reason: 'controller lock metadata is invalid; manual inspection is required', info };
  }
  try {
    process.kill(owner.pid, 0);
  } catch (error) {
    if (error.code === 'ESRCH') return { stale: true, ownerless: false, info, owner };
    return { stale: false, reason: 'cannot establish whether the controller lock owner is alive', info };
  }
  try {
    const liveIdentity = processIdentity(owner.pid);
    if (liveIdentity !== owner.identity) return { stale: true, ownerless: false, info, owner };
    return { stale: false, reason: 'another controller owns this SpeechCraft installation', info, owner };
  } catch {
    // A live PID whose start identity cannot be checked is never evicted.
    return { stale: false, reason: 'cannot safely identify the controller lock owner', info };
  }
}

function sameFileIdentity(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function acquireOwnership(directory) {
  const lockPath = path.join(directory, '.controller.lock');
  const recoveryPath = path.join(directory, '.controller.recovery');
  const owner = {
    version: OWNER_VERSION,
    pid: process.pid,
    identity: processIdentity(process.pid),
    nonce: randomBytes(24).toString('hex'),
    createdAt: new Date().toISOString(),
  };
  const writeExclusive = (target, value) => {
    const fd = fs.openSync(target, 'wx', 0o600);
    try {
      fs.writeFileSync(fd, `${JSON.stringify(value)}\n`, 'utf8');
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    syncDirectory(directory);
    return fs.lstatSync(target);
  };
  const recoveryBlocker = () => {
    if (!fs.existsSync(recoveryPath)) return false;
    let reason = 'a controller is recovering stale ownership';
    try {
      const info = fs.lstatSync(recoveryPath);
      if (!info.isFile() || info.isSymbolicLink()
          || (typeof process.getuid === 'function' && info.uid !== process.getuid())
          || (info.mode & 0o077) !== 0 || info.size > 4_096) reason = 'controller recovery metadata is unsafe';
      else {
        const recovery = JSON.parse(fs.readFileSync(recoveryPath, 'utf8'));
        if (!Number.isSafeInteger(recovery.pid) || typeof recovery.identity !== 'string') reason = 'controller recovery metadata is corrupt';
      }
    } catch { reason = 'controller recovery metadata is corrupt'; }
    throw new StateStoreError('LOCK_RECOVERY_REQUIRED', `${reason}; preserve the lock files and inspect ${directory} before manual recovery`);
  };
  recoveryBlocker();
  try {
    const info = writeExclusive(lockPath, owner);
    // A stale-lock reclaimer can only create its marker after observing an old
    // lock. Recheck after creation so a contender never proceeds through it.
    if (fs.existsSync(recoveryPath)) {
      const current = fs.lstatSync(lockPath);
      if (sameFileIdentity(info, current)) fs.unlinkSync(lockPath);
      throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', 'another controller is recovering ownership; retry after it exits');
    }
    return { lockPath, recoveryPath, owner, fileIdentity: info };
  } catch (error) {
    if (error.code !== 'EEXIST') throw error;
  }

  let status;
  try { status = inspectOwner(lockPath); }
  catch (error) { throw error; }
  if (!status.stale) throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', status.reason);

  const recovery = { version: OWNER_VERSION, pid: process.pid, identity: owner.identity,
    nonce: randomBytes(24).toString('hex'), createdAt: owner.createdAt };
  let recoveryInfo;
  try { recoveryInfo = writeExclusive(recoveryPath, recovery); }
  catch (error) {
    if (error.code === 'EEXIST') throw new StateStoreError('LOCK_RECOVERY_REQUIRED', 'another controller is recovering stale ownership; retry after it exits');
    throw error;
  }
  let newInfo;
  try {
    const current = inspectOwner(lockPath);
    if (!sameFileIdentity(status.info, current.info) || !current.stale
        || current.owner?.nonce !== status.owner?.nonce) {
      throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', 'controller ownership changed during stale-lock recovery; retry after inspecting the owner');
    }
    // The exclusive recovery marker prevents compliant contenders from
    // acquiring the now-vacant lock path. The inode check above means this
    // unlink can only remove the stale file that was inspected.
    const beforeUnlink = fs.lstatSync(lockPath);
    if (!sameFileIdentity(status.info, beforeUnlink)) {
      throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', 'controller lock changed during recovery; no ownership was removed');
    }
    fs.unlinkSync(lockPath);
    try { newInfo = writeExclusive(lockPath, owner); }
    catch (error) {
      if (error.code === 'EEXIST') throw new StateStoreError('CONTROLLER_ALREADY_RUNNING', 'another controller acquired ownership during recovery');
      throw error;
    }
    // We created the lock while holding the recovery marker. No other
    // compliant contender may treat it as stale until the marker is removed.
    const ownerCheck = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
    if (ownerCheck.nonce !== owner.nonce) throw new StateStoreError('LOCK_RECOVERY_FAILED', 'recovered lock identity changed unexpectedly');
    fs.unlinkSync(recoveryPath);
    syncDirectory(directory);
    return { lockPath, recoveryPath, owner, fileIdentity: newInfo };
  } catch (error) {
    // If this process dies before this cleanup, the marker remains as a
    // fail-closed recovery tombstone. It is never guessed stale or stolen.
    try {
      if (newInfo) {
        const currentLock = fs.lstatSync(lockPath);
        if (sameFileIdentity(newInfo, currentLock)
            && JSON.parse(fs.readFileSync(lockPath, 'utf8')).nonce === owner.nonce) fs.unlinkSync(lockPath);
      }
    } catch {}
    try {
      const marker = fs.lstatSync(recoveryPath);
      if (sameFileIdentity(recoveryInfo, marker)) {
        const stored = JSON.parse(fs.readFileSync(recoveryPath, 'utf8'));
        if (stored.nonce === recovery.nonce) fs.unlinkSync(recoveryPath);
      }
    } catch {}
    throw error;
  }
}

const JOB_STATES = new Set(['running', 'waiting_for_user', 'cancelling', 'failed', 'cancelled', 'interrupted', 'completed']);

function validateState(state, version = STATE_VERSION) {
  const jobVersion = version === 1 ? 1 : 2;
  if (!state || state.schema_version !== version || !Number.isSafeInteger(state.revision)
      || state.revision < 0 || !Array.isArray(state.events) || state.events.length > EVENT_LIMIT
      || !(state.job === null || (typeof state.job === 'object' && !Array.isArray(state.job)))
      || (state.revision === 0 && state.events.length !== 0)
      || (state.revision > 0 && (!state.events.length || state.events.at(-1)?.id !== state.revision))) {
    throw new StateStoreError('STATE_INVALID', 'setup state has an unsupported or invalid schema');
  }
  if (state.job !== null) {
    const job = state.job;
    if (job.schema_version !== jobVersion || typeof job.id !== 'string'
        || (version === 2 && !/^[0-9a-f-]{36}$/i.test(job.attempt_id || ''))
        || !JOB_STATES.has(job.state)
        || !Number.isSafeInteger(job.attempt_count) || job.attempt_count < 1
        || !Number.isSafeInteger(job.revision) || job.revision !== state.revision
        || typeof job.created_at !== 'string' || typeof job.updated_at !== 'string'
        || (job.stage !== null && typeof job.stage !== 'string')
        || (job.task_id !== null && typeof job.task_id !== 'string')
        || (job.error !== null && (typeof job.error.code !== 'string' || typeof job.error.message !== 'string'))) {
      throw new StateStoreError('STATE_INVALID', 'setup state job record is malformed');
    }
  }
  if (state.service_process !== undefined && state.service_process !== null) {
    const service = state.service_process;
    if (!service || service.kind !== 'speechcraft-backend' || typeof service.nonce !== 'string'
        || !/^[a-f0-9-]{36}$/i.test(service.attempt_id || '')
        || (service.pid !== null && (!Number.isSafeInteger(service.pid) || service.pid < 2))
        || (service.process_group_id !== null && (!Number.isSafeInteger(service.process_group_id) || service.process_group_id < 2))
        || (service.identity !== null && typeof service.identity !== 'string')
        || !['starting', 'running', 'stopping', 'termination_uncertain'].includes(service.state) || typeof service.created_at !== 'string'
        || !Number.isSafeInteger(service.port) || service.port < 1 || service.port > 65535) {
      throw new StateStoreError('STATE_INVALID', 'persisted backend service ownership metadata is malformed');
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

function journalJob(job) {
  if (!job) return null;
  const snapshot = structuredClone(job);
  // Full component reports remain in the authoritative job. Bounded event
  // history stores only compact job state plus component deltas below.
  delete snapshot.bootstrap_status;
  delete snapshot.child_process;
  delete snapshot.python_executable;
  if (snapshot.progress?.message) snapshot.progress.message = snapshot.progress.message.slice(0, 320);
  return snapshot;
}

function componentDeltas(previous, next) {
  const before = previous?.bootstrap_status?.components || {};
  const after = next?.bootstrap_status?.components || {};
  const updates = {};
  for (const [name, value] of Object.entries(after)) {
    const compact = (item) => ({
      identity: typeof item?.identity === 'string' ? item.identity.slice(0, 160) : null,
      state: item?.state || 'pending', verification: item?.verification || 'pending',
      last_verified_at: item?.last_verified_at || null,
      failure_code: item?.failure_code || null,
      message: typeof item?.message === 'string' ? item.message.slice(0, 320) : null,
      runtime_ready: item?.runtime_ready === true,
      ...(Number.isSafeInteger(item?.port) ? { port: item.port } : {}),
    });
    const updated = compact(value);
    if (JSON.stringify(compact(before[name])) === JSON.stringify(updated)) continue;
    updates[name] = updated;
  }
  return updates;
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
      if (store.state.schema_version !== STATE_VERSION) {
        store.state = migrateState(store.state);
        atomicWriteFile(store.directory, 'state.json', `${JSON.stringify(store.state, null, 2)}\n`);
      }
      return store;
    } catch (error) {
      store.releaseOwnership();
      throw error;
    }
  }

  read() {
    if (!safeRegularFile(this.file)) {
      if (fs.existsSync(this.file)) throw new StateStoreError('STATE_UNSAFE', 'setup state must be a regular non-symlink file');
      return { schema_version: STATE_VERSION, revision: 0, job: null, service_process: null, events: [] };
    }
    const fileInfo = fs.lstatSync(this.file);
    if ((typeof process.getuid === 'function' && fileInfo.uid !== process.getuid())
        || (fileInfo.mode & 0o077) !== 0 || fileInfo.size > 1_048_576) {
      throw new StateStoreError('STATE_UNSAFE', 'setup state file ownership, permissions, or size are unsafe');
    }
    let state;
    try { state = JSON.parse(fs.readFileSync(this.file, 'utf8')); }
    catch { throw new StateStoreError('STATE_CORRUPT', 'setup state JSON is corrupt; preserve the file and inspect it before recovery'); }
    if (state?.schema_version === 1) validateState(state, 1);
    else validateState(state);
    return state;
  }

  snapshot() { return structuredClone(this.state); }

  commit(mutator, type) {
    const previous = this.state;
    const next = structuredClone(this.state);
    mutator(next);
    next.revision += 1;
    const now = new Date().toISOString();
    if (next.job) {
      next.job.updated_at = now;
      next.job.revision = next.revision;
    }
    const updates = componentDeltas(previous.job, next.job);
    next.events.push({ id: next.revision, type, at: now, data: {
      job: journalJob(next.job), ...(Object.keys(updates).length ? { component_updates: updates } : {}),
    } });
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
    const { lockPath, owner, fileIdentity } = this.lock;
    try {
      const info = fs.lstatSync(lockPath);
      if (!info.isFile() || info.isSymbolicLink() || !sameFileIdentity(fileIdentity, info)) return;
      const current = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
      if (current.nonce !== owner.nonce) return;
      fs.unlinkSync(lockPath);
      syncDirectory(this.directory);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
}

function migrateState(state) {
  const migrateJob = (job) => job ? { ...job, schema_version: 2, attempt_id: randomUUID() } : null;
  return {
    ...state,
    schema_version: STATE_VERSION,
    job: migrateJob(state.job),
    events: state.events.map((event) => ({ ...event,
      data: { ...event.data, job: migrateJob(event.data.job) } })),
  };
}
