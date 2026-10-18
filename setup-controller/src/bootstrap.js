import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { gunzipSync } from 'node:zlib';
import { inspectHost, validateManagedStorage } from './platform.js';
import { DownloadProgressTracker } from './download-progress.js';
import { PRODUCTION_STAGES, assertTrustedBackendRecipe, expectedWorkerMachines, hashMatches, initialComponents, runtimeRoots, runtimeManifest, selectRuntimeProfile } from './runtime-plan.js';

const manifest = JSON.parse(fs.readFileSync(fileURLToPath(new URL('../resources/bootstrap-manifest.json', import.meta.url)), 'utf8'));

export const BOOTSTRAP_STAGES = Object.freeze([
  'inspect_system', 'install_uv', 'install_python', 'install_model_manager', 'validate_bootstrap',
]);

const REQUIREMENTS = fileURLToPath(new URL('../resources/model-manager-requirements.txt', import.meta.url));
const MAX_UV_ARCHIVE_BYTES = 32 * 1024 * 1024;
const MAX_UV_EXPANDED_BYTES = 64 * 1024 * 1024;
const OUTPUT_CAPTURE_LIMIT = 64 * 1024;
const CHILD_TERMINATE_GRACE_MS = 2_500;
const PYTHON_PROBE = String.raw`import json, os, platform, ssl, sys, sysconfig, venv
import hashlib, importlib, pathlib
assert sys.version_info[:3] == (3, 12, 15), sys.version
for module in (json, os, platform, ssl, sys, sysconfig, venv, hashlib, importlib, pathlib): assert module
print(json.dumps({"kind":"SPEECHCRAFT_PYTHON_PROBE","version":".".join(map(str,sys.version_info[:3])),"machine":platform.machine(),"system":platform.system(),"executable":os.path.realpath(sys.executable),"prefix":os.path.realpath(sys.prefix),"base_prefix":os.path.realpath(sys.base_prefix)}))`;
const MANAGER_PROBE = String.raw`import base64, csv, hashlib, importlib.metadata as md, io, json, os, platform, stat, sys, zipfile
from email.parser import Parser

class ProbeFailure(Exception):
    def __init__(self, code): self.code = code

def safe_relative(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    parts = value.rstrip("/").split("/")
    return bool(parts) and ":" not in parts[0] and all(part not in ("", ".", "..") for part in parts)

def within(root, target):
    try: return os.path.commonpath((root, target)) == root
    except ValueError: return False

def regular_without_symlink_parents(root, relative):
    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode): return False
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode): return False
        if index == len(parts) - 1 and not stat.S_ISREG(info.st_mode): return False
    return True

def read_regular_no_follow(path, failure_code="MODEL_MANAGER_INSTALLED_FILES_MISMATCH"):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProbeFailure(failure_code)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0: os.close(descriptor)

def directory_without_symlink_parents(root, relative):
    current = root
    for part in relative.split("/"):
        current = os.path.join(current, part)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode): return False
    return True

def installed_relative(name):
    parts = name.split("/")
    if parts[0].endswith(".data"):
        if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
            raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        return "/".join(parts[2:])
    return name

def generated_installer_file(relative, dist_info):
    if relative in {dist_info + "/direct_url.json", dist_info + "/INSTALLER", dist_info + "/REQUESTED", dist_info + "/uv_cache.json"}:
        return True
    parts = relative.split("/")
    return "__pycache__" in parts and relative.endswith(".pyc")

def verify_wheel_and_install(wheel_path, trusted_sha):
    try:
        wheel_info = os.lstat(wheel_path)
        if not stat.S_ISREG(wheel_info.st_mode) or stat.S_ISLNK(wheel_info.st_mode):
            raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        wheel_bytes = read_regular_no_follow(wheel_path, "MODEL_MANAGER_WHEEL_INVALID")
        wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()
        if wheel_sha != trusted_sha:
            return {"wheel_sha256": wheel_sha, "trusted_wheel_valid": False}
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            wheel_files = {}
            names = set()
            for info in archive.infolist():
                raw_name = info.filename[:-1] if info.is_dir() else info.filename
                if not safe_relative(raw_name) or info.filename in names:
                    raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
                names.add(info.filename)
                mode = (info.external_attr >> 16) & 0xffff
                kind = stat.S_IFMT(mode)
                if info.is_dir():
                    if kind not in (0, stat.S_IFDIR): raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
                    continue
                if kind not in (0, stat.S_IFREG): raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
                wheel_files[raw_name] = archive.read(info)

        record_names = [name for name in wheel_files if name.endswith(".dist-info/RECORD")]
        metadata_names = [name for name in wheel_files if name.endswith(".dist-info/METADATA")]
        if len(record_names) != 1 or len(metadata_names) != 1:
            raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        dist_info = metadata_names[0].rsplit("/", 1)[0]
        record_name = record_names[0]
        if record_name.rsplit("/", 1)[0] != dist_info:
            raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        rows = {}
        for row in csv.reader(io.StringIO(wheel_files[record_name].decode("utf-8"))):
            if len(row) != 3 or not safe_relative(row[0]) or row[0] in rows:
                raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
            rows[row[0]] = (row[1], row[2])
        if set(rows) != set(wheel_files): raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        trusted_installed = {}
        for name, content in wheel_files.items():
            encoded_hash, recorded_size = rows[name]
            if name == record_name:
                if encoded_hash or recorded_size: raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
            else:
                if not encoded_hash.startswith("sha256=") or not recorded_size.isdigit():
                    raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
                expected_hash = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode("ascii").rstrip("=")
                if encoded_hash[7:] != expected_hash or int(recorded_size) != len(content):
                    raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
            installed_name = installed_relative(name)
            if installed_name in trusted_installed: raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
            trusted_installed[installed_name] = (content, rows[name])

        dist = md.distribution("speechcraft-model-manager")
        metadata_path_raw = os.path.abspath(os.fspath(dist.locate_file(dist_info + "/METADATA")))
        site_root_raw = os.path.abspath(os.fspath(dist.locate_file("")))
        site_root = os.path.realpath(site_root_raw)
        prefix = os.path.realpath(sys.prefix)
        if not within(prefix, site_root) or site_root_raw != site_root \
                or os.path.basename(os.fspath(dist._path)) != dist_info \
                or metadata_path_raw != os.path.join(site_root, dist_info, "METADATA"):
            raise ProbeFailure("MODEL_MANAGER_ENVIRONMENT_PATH_MISMATCH")
        if not directory_without_symlink_parents(prefix, os.path.relpath(site_root, prefix).replace(os.sep, "/")):
            raise ProbeFailure("MODEL_MANAGER_ENVIRONMENT_PATH_MISMATCH")

        if not regular_without_symlink_parents(site_root, dist_info + "/METADATA"):
            raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
        installed_metadata = Parser().parsestr(read_regular_no_follow(metadata_path_raw).decode("utf-8"))
        wheel_metadata = Parser().parsestr(wheel_files[metadata_names[0]].decode("utf-8"))
        if wheel_metadata.get("Name", "").lower().replace("_", "-") != "speechcraft-model-manager":
            raise ProbeFailure("MODEL_MANAGER_WHEEL_INVALID")
        if installed_metadata.get("Version") != wheel_metadata.get("Version"):
            raise ProbeFailure("MODEL_MANAGER_PACKAGE_VERSION_MISMATCH")
        if installed_metadata.get("Name", "").lower().replace("_", "-") != "speechcraft-model-manager":
            raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
        version = installed_metadata.get("Version")

        installed_record_path = os.path.join(site_root, dist_info, "RECORD")
        if not regular_without_symlink_parents(site_root, dist_info + "/RECORD"):
            raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
        installed_rows = {}
        for row in csv.reader(io.StringIO(read_regular_no_follow(installed_record_path).decode("utf-8"))):
            if len(row) != 3 or not safe_relative(row[0]) or row[0] in installed_rows:
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
            installed_rows[row[0]] = (row[1], row[2])
        for name in installed_rows:
            if name not in trusted_installed and not generated_installer_file(name, dist_info):
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
        for name, (content, (expected_hash, expected_size)) in trusted_installed.items():
            if name == dist_info + "/RECORD": continue
            actual_hash, actual_size = installed_rows.get(name, (None, None))
            if (actual_hash, actual_size) != (expected_hash, expected_size):
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
            if not regular_without_symlink_parents(site_root, name):
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
            path = os.path.join(site_root, *name.split("/"))
            actual = hashlib.sha256(read_regular_no_follow(path)).digest()
            if len(content) != int(expected_size) or actual != hashlib.sha256(content).digest():
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")

        expected_roots = {name.split("/", 1)[0] for name in trusted_installed}
        for root_name in expected_roots:
            root_path = os.path.join(site_root, root_name)
            root_info = os.lstat(root_path)
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
            for current, directories, files in os.walk(root_path, followlinks=False):
                for child in directories:
                    info = os.lstat(os.path.join(current, child))
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                        raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
                for child in files:
                    file_path = os.path.join(current, child)
                    info = os.lstat(file_path)
                    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                        raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
                    relative = os.path.relpath(file_path, site_root).replace(os.sep, "/")
                    if relative not in trusted_installed and not generated_installer_file(relative, dist_info):
                        raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")

        return {"wheel_sha256": wheel_sha, "trusted_wheel_valid": True, "version": version,
            "prefix": prefix, "base_prefix": os.path.realpath(sys.base_prefix),
            "python": platform.python_version(), "site_root": site_root,
            "dist_info": dist_info, "trusted_installed": trusted_installed}
    except ProbeFailure:
        raise
    except Exception:
        raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")

result = {"kind": "SPEECHCRAFT_MODEL_MANAGER_PROBE"}
try:
    wheel_path, trusted_sha = sys.argv[1:3]
    verified = verify_wheel_and_install(wheel_path, trusted_sha)
    result.update({key: value for key, value in verified.items() if key != "trusted_installed"})
    if not verified.get("trusted_wheel_valid"):
        result["failure_code"] = "MODEL_MANAGER_WHEEL_HASH_MISMATCH"
    else:
        try:
            import speechcraft_models
            from speechcraft_models.manifest import load_catalog, validate_manifest
            expected_init = os.path.join(verified["site_root"], "speechcraft_models", "__init__.py")
            if os.path.realpath(speechcraft_models.__file__) != expected_init:
                raise ProbeFailure("MODEL_MANAGER_INSTALLED_FILES_MISMATCH")
            catalog = load_catalog(); validate_manifest(catalog)
            roles = sorted(item["role"] for item in catalog["models"])
            required = {"diarization", "asr", "speech_detection", "speaker_purity"}
            result["roles"] = roles
            result["catalog_valid"] = required.issubset(set(roles))
            if not result["catalog_valid"]: result["failure_code"] = "MODEL_MANAGER_CATALOG_INVALID"
        except ProbeFailure as error:
            result["catalog_valid"] = False
            result["failure_code"] = error.code
        except Exception:
            result["catalog_valid"] = False
            result["failure_code"] = "MODEL_MANAGER_CATALOG_INVALID"
        forbidden = {"torch", "pytorch", "pyannote.audio", "faster-whisper", "mlx", "mlx-whisper", "nemo-toolkit", "sherpa-onnx"}
        installed = {d.metadata.get("Name", "").lower().replace("_", "-") for d in md.distributions()}
        result["heavy_dependencies"] = sorted(installed & forbidden)
except ProbeFailure as error:
    result["failure_code"] = error.code
except Exception:
    result["failure_code"] = "MODEL_MANAGER_PROBE_FAILED"
print(json.dumps(result, sort_keys=True))`;

export class BootstrapError extends Error {
  constructor(code, message, options = {}) {
    super(message, options);
    this.name = 'BootstrapError';
    this.code = code;
  }
}

function assertInside(root, target) {
  const relative = path.relative(root, target);
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'A bootstrap path resolved outside SpeechCraft-managed storage.');
  }
}

export function ensureManagedDirectory(root, target) {
  const resolvedRoot = fs.realpathSync(root);
  const absolute = path.resolve(target);
  assertInside(resolvedRoot, absolute);
  const relative = path.relative(resolvedRoot, absolute);
  let current = resolvedRoot;
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    try { fs.mkdirSync(current, { mode: 0o700 }); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) {
      throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'A managed bootstrap directory contains a symlink or non-directory entry.');
    }
    assertInside(resolvedRoot, fs.realpathSync(current));
  }
  return absolute;
}

function validateNoSymlinks(directory) {
  const info = fs.lstatSync(directory);
  if (info.isSymbolicLink()) throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'Refusing to clean a staging path containing a symlink.');
  if (info.isDirectory()) {
    for (const name of fs.readdirSync(directory)) validateNoSymlinks(path.join(directory, name));
  }
}

function removeOwnedDirectory(root, target) {
  const absolute = path.resolve(target);
  assertInside(root, absolute);
  if (!fs.existsSync(absolute)) return;
  validateNoSymlinks(absolute);
  fs.rmSync(absolute, { recursive: true, force: false });
}

function parseOctal(field) {
  const value = field.toString('ascii').replace(/\0.*$/s, '').trim();
  if (!/^[0-7]+$/.test(value)) throw new BootstrapError('UV_ARCHIVE_INVALID', 'The trusted uv archive contains an invalid tar header.');
  return Number.parseInt(value, 8);
}

export function extractUvArchive(archiveBytes, artifact) {
  let tar;
  try { tar = gunzipSync(archiveBytes, { maxOutputLength: MAX_UV_EXPANDED_BYTES }); }
  catch (error) { throw new BootstrapError('UV_ARCHIVE_INVALID', 'The verified uv archive could not be safely decompressed.', { cause: error }); }
  const expectedNames = new Set([
    `${artifact.tar_root}/`, `${artifact.tar_root}/uv`, `${artifact.tar_root}/uvx`,
  ]);
  const files = new Map();
  for (let offset = 0; offset + 512 <= tar.length;) {
    const header = tar.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const rawName = header.subarray(0, 100).toString('utf8').replace(/\0.*$/s, '');
    const rawPrefix = header.subarray(345, 500).toString('utf8').replace(/\0.*$/s, '');
    const name = rawPrefix ? `${rawPrefix}/${rawName}` : rawName;
    if (!expectedNames.has(name)) throw new BootstrapError('UV_ARCHIVE_UNSAFE', 'The uv archive contains an unexpected path.');
    const type = String.fromCharCode(header[156] || 48);
    const size = parseOctal(header.subarray(124, 136));
    if (name === `${artifact.tar_root}/`) {
      if (type !== '5' || size !== 0) throw new BootstrapError('UV_ARCHIVE_UNSAFE', 'The uv archive root entry is not a plain directory.');
    } else {
      if (!['0', '\0'].includes(type) || size <= 0 || size > MAX_UV_EXPANDED_BYTES) {
        throw new BootstrapError('UV_ARCHIVE_UNSAFE', 'The uv archive contains a link, special file, or invalid file size.');
      }
      if (offset + 512 + size > tar.length) throw new BootstrapError('UV_ARCHIVE_INVALID', 'The uv archive is truncated.');
      files.set(name, Buffer.from(tar.subarray(offset + 512, offset + 512 + size)));
    }
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  const uv = files.get(`${artifact.tar_root}/uv`);
  const uvx = files.get(`${artifact.tar_root}/uvx`);
  if (!uv || !uvx) throw new BootstrapError('UV_ARCHIVE_INVALID', 'The uv archive did not contain both official executable entries.');
  if (artifact.executable_size_bytes !== null && uv.byteLength !== artifact.executable_size_bytes) {
    throw new BootstrapError('UV_ARCHIVE_INVALID', 'The uv executable size does not match the trusted manifest.');
  }
  return uv;
}

export function ownedProcessIdentity(pid, nonce = null) {
  if (process.platform === 'linux') {
    try {
      const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
      const end = stat.lastIndexOf(')');
      const fields = stat.slice(end + 2).trim().split(/\s+/);
      const processGroup = Number(fields[2]);
      const startTicks = fields[19];
      const bootId = fs.readFileSync('/proc/sys/kernel/random/boot_id', 'utf8').trim();
      return startTicks && bootId && processGroup === pid ? `linux:${bootId}:${startTicks}:${processGroup}` : null;
    } catch { return null; }
  }
  if (process.platform === 'darwin') {
    // Darwin's `lstart` has one-second resolution. A launch nonce in the
    // owned process environment prevents a reused PID from being mistaken
    // for the process SpeechCraft started.
    if (!nonce) return null;
    try {
      const details = execFileSync('/bin/ps', ['eww', '-p', String(pid)], {
        encoding: 'utf8', timeout: 2_000, stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (!details.includes(`SPEECHCRAFT_OWNED_PROCESS_NONCE=${nonce}`)) return null;
      const processGroup = execFileSync('/bin/ps', ['-o', 'pgid=', '-p', String(pid)], {
        encoding: 'utf8', timeout: 2_000, stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      if (processGroup !== String(pid)) return null;
      const started = execFileSync('/bin/ps', ['-o', 'lstart=', '-p', String(pid)], {
        encoding: 'utf8', timeout: 2_000, stdio: ['ignore', 'pipe', 'ignore'],
      }).trim();
      return started ? `darwin:${started}:${processGroup}:${nonce}` : null;
    } catch { return null; }
  }
  return null;
}

function signalProcessGroup(pid, signal) {
  try { process.kill(-pid, signal); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; throw error; }
}

function matchesProcessIdentity(record, currentIdentity) {
  if (!currentIdentity || typeof record?.identity !== 'string') return false;
  if (currentIdentity === record.identity) return true;
  // Records created before process-group IDs were included remain verifiable
  // on Linux: the live identity still proves boot ID/start ticks and the
  // current /proc check independently confirms pgrp === pid.
  return process.platform === 'linux' && /^linux:[^:]+:\d+$/.test(record.identity)
    && currentIdentity.startsWith(`${record.identity}:`);
}

function waitForProcessGroupExit(pid, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve) => {
    const poll = () => {
      try { process.kill(-pid, 0); }
      catch (error) { if (error.code === 'ESRCH') { resolve(true); return; } }
      if (Date.now() >= deadline) { resolve(false); return; }
      setTimeout(poll, 50);
    };
    poll();
  });
}

export async function terminateRecoveredProcess(record, { graceMs = CHILD_TERMINATE_GRACE_MS } = {}) {
  if (!record || !Number.isSafeInteger(record.pid) || record.pid < 2
      || typeof record.identity !== 'string' || typeof record.attempt_id !== 'string'
      || (record.process_group_id !== undefined && record.process_group_id !== null
        && record.process_group_id !== record.pid)
      || (process.platform === 'darwin' && typeof record.nonce !== 'string')) {
    throw new BootstrapError('BOOTSTRAP_PROCESS_IDENTITY_UNKNOWN', 'An interrupted bootstrap process has incomplete ownership metadata; inspect it before retrying.');
  }
  const currentIdentity = ownedProcessIdentity(record.pid, record.nonce);
  if (currentIdentity === null) {
    try { process.kill(-record.pid, 0); }
    catch (error) { if (error.code === 'ESRCH') return { stopped: true, alreadyExited: true }; }
    throw new BootstrapError('BOOTSTRAP_PROCESS_IDENTITY_UNKNOWN', 'The interrupted bootstrap process exited but its process group may still contain writers; inspect it before retrying.');
  }
  if (!matchesProcessIdentity(record, currentIdentity)) {
    throw new BootstrapError('BOOTSTRAP_PROCESS_IDENTITY_CHANGED', 'A bootstrap process ID was reused before recovery could confirm its owner; stop and inspect the managed installation before retrying.');
  }
  try { signalProcessGroup(record.pid, 'SIGTERM'); }
  catch { throw new BootstrapError('BOOTSTRAP_PROCESS_STOP_FAILED', 'Could not signal the interrupted bootstrap process group safely.'); }
  if (await waitForProcessGroupExit(record.pid, graceMs)) return { stopped: true, alreadyExited: false };
  // Once the leader exits, the numeric process-group ID may eventually be
  // reused. Escalation is safe only while the original leader identity lives.
  if (!matchesProcessIdentity(record, ownedProcessIdentity(record.pid, record.nonce))) {
    throw new BootstrapError('BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'The bootstrap leader exited but its process group still has members; ownership is retained and retry is blocked. Inspect the recorded process group before continuing.');
  }
  try { signalProcessGroup(record.pid, 'SIGKILL'); }
  catch { throw new BootstrapError('BOOTSTRAP_PROCESS_STOP_FAILED', 'Could not force-stop the interrupted bootstrap process group.'); }
  if (!await waitForProcessGroupExit(record.pid, graceMs)) {
    throw new BootstrapError('BOOTSTRAP_PROCESS_STOP_TIMEOUT', 'The interrupted bootstrap process group did not stop; retry is blocked to protect managed files.');
  }
  return { stopped: true, alreadyExited: false };
}

export async function reconcileOwnedProcess(store) {
  const record = store.snapshot().job?.child_process;
  if (!record) return { status: 'none' };
  try {
    await terminateRecoveredProcess(record);
    store.commit((state) => {
      if (state.job?.attempt_id !== record.attempt_id) return;
      state.job.child_process = null;
      state.job.termination_status = 'termination_confirmed';
    }, 'bootstrap_process_termination_confirmed');
    return { status: 'termination_confirmed' };
  } catch (error) {
    store.commit((state) => {
      if (state.job?.attempt_id !== record.attempt_id) return;
      state.job.child_process = { ...record, termination_status: 'termination_uncertain',
        diagnostic: error.message.slice(0, 320) };
      state.job.termination_status = 'termination_uncertain';
      if (['interrupted', 'failed', 'cancelled'].includes(state.job.state)) {
        state.job.error = { code: error.code || 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN',
          message: error.message.slice(0, 320) };
      }
    }, 'bootstrap_process_termination_uncertain');
    throw error;
  }
}

export function runOwnedProcess(executable, args, { signal, cwd, env, stdinData, onChildProcess, onOutput,
  graceMs = CHILD_TERMINATE_GRACE_MS, maxOutputBytes = OUTPUT_CAPTURE_LIMIT,
  identityResolver = ownedProcessIdentity } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new BootstrapError('BOOTSTRAP_CANCELLED', 'Bootstrap was cancelled.')); return; }
    const launchNonce = randomUUID();
    let child;
    try {
      child = spawn(executable, args, { cwd,
        env: { ...(env || process.env), SPEECHCRAFT_OWNED_PROCESS_NONCE: launchNonce }, shell: false, detached: process.platform !== 'win32',
        windowsHide: true, stdio: [stdinData === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'] });
    } catch (error) {
      reject(new BootstrapError('BOOTSTRAP_PROCESS_START_FAILED', 'Could not start a trusted bootstrap operation.', { cause: error }));
      return;
    }
    const collected = { stdout: [], stderr: [], stdoutBytes: 0, stderrBytes: 0 };
    const secretInput = stdinData === undefined ? null
      : Buffer.isBuffer(stdinData) ? stdinData : Buffer.from(String(stdinData));
    const discardSecret = () => { if (secretInput) secretInput.fill(0); };
    const closeCredentialPipe = () => {
      if (!child?.stdin || child.stdin.destroyed || child.stdin.writableEnded) return;
      child.stdin.destroy();
    };
    const capture = (stream, field, byteField) => stream.on('data', (chunk) => {
      try { onOutput?.(field, Buffer.from(chunk)); } catch {}
      const remaining = maxOutputBytes - collected[byteField];
      if (remaining > 0) {
        const kept = Buffer.from(chunk.subarray(0, remaining));
        collected[field].push(kept);
        collected[byteField] += kept.length;
      }
    });
    capture(child.stdout, 'stdout', 'stdoutBytes');
    capture(child.stderr, 'stderr', 'stderrBytes');
    let childRecord = null;
    let spawnError = null;
    let killTimer = null;
    let uncertainTimer = null;
    let cancelled = false;
    let terminationStarted = false;
    let settled = false;
    let ownershipWriteFailed = false;
    let ownershipUpdate = Promise.resolve();
    const markTerminationUncertain = async (diagnostic) => {
      if (settled) return;
      settled = true;
      discardSecret();
      closeCredentialPipe();
      signal?.removeEventListener('abort', onAbort);
      try {
        if (childRecord) await onChildProcess?.({ ...childRecord,
          termination_status: 'termination_uncertain', diagnostic });
      } catch { ownershipWriteFailed = true; }
      reject(new BootstrapError('BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN',
        `${diagnostic} Ownership is retained and retry is blocked.`));
    };
    const stop = async () => {
      cancelled = true;
      // Abort can race the `spawn` event. Never signal until durable ownership
      // exists and the PID still identifies the process we created.
      if (!childRecord || terminationStarted) return;
      try { await ownershipUpdate; }
      catch {
        // The child is still ours because its current OS identity is checked
        // below. Close its private input first, then terminate only that
        // verified process group. If cleanup cannot be confirmed, retain an
        // uncertainty record so retries remain blocked.
        ownershipWriteFailed = true;
      }
      if (settled || terminationStarted) return;
      if (!matchesProcessIdentity(childRecord, identityResolver(childRecord.pid, childRecord.nonce))) {
        await markTerminationUncertain('The owned process identity is missing or changed; no signal was sent.');
        return;
      }
      terminationStarted = true;
      const initialIdentity = identityResolver(childRecord.pid, childRecord.nonce);
      if (!matchesProcessIdentity(childRecord, initialIdentity)) {
        await markTerminationUncertain('The owned process identity changed before initial SIGTERM; no signal was sent.');
        return;
      }
      try { signalProcessGroup(childRecord.pid, 'SIGTERM'); }
      catch (error) {
        await markTerminationUncertain(`Initial process-group SIGTERM could not be confirmed (${error.code || 'signal error'}).`);
        return;
      }
      killTimer = setTimeout(async () => {
        if (!childRecord || !matchesProcessIdentity(childRecord, identityResolver(childRecord.pid, childRecord.nonce))) {
          await markTerminationUncertain('The owned process identity changed before SIGKILL escalation; no signal was sent.');
          return;
        }
        try { signalProcessGroup(childRecord.pid, 'SIGKILL'); }
        catch (error) {
          await markTerminationUncertain(`Process-group SIGKILL could not be confirmed (${error.code || 'signal error'}).`);
          return;
        }
        uncertainTimer = setTimeout(async () => {
          if (settled) return;
          // If ChildProcess has not delivered `close`, we do not have a
          // complete termination/reap acknowledgement, even if a group probe
          // happens to look empty. Keep ownership and fail closed.
          settled = true;
          signal?.removeEventListener('abort', onAbort);
          try {
            if (childRecord) await onChildProcess?.({ ...childRecord,
              termination_status: 'termination_uncertain',
              diagnostic: 'Process-group termination could not be confirmed after SIGKILL.' });
          } catch { ownershipWriteFailed = true; }
          reject(new BootstrapError('BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN',
            'The bootstrap process group did not stop conclusively after termination escalation; ownership is retained and retry is blocked.'));
        }, graceMs);
        uncertainTimer.unref?.();
      }, graceMs);
      killTimer.unref?.();
    };
    const onAbort = () => { void stop(); };
    signal?.addEventListener('abort', onAbort, { once: true });
    child.once('error', (error) => { spawnError = error; });
    child.once('spawn', () => {
      childRecord = { pid: child.pid, process_group_id: child.pid, identity: identityResolver(child.pid, launchNonce),
        nonce: launchNonce, attempt_id: null, termination_status: 'active' };
      ownershipUpdate = Promise.resolve().then(() => {
        if (secretInput && typeof onChildProcess !== 'function') {
          throw new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'A credential-bearing process requires durable ownership acknowledgement.');
        }
        return onChildProcess?.(childRecord, 'persist');
      });
      ownershipUpdate.then(async (ownershipAcknowledged) => {
        if (secretInput ? ownershipAcknowledged !== true : ownershipAcknowledged === false) {
          throw new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'The setup attempt did not explicitly acknowledge durable process ownership; no credential was released.');
        }
        if (!childRecord.identity) {
          discardSecret();
          closeCredentialPipe();
          void markTerminationUncertain('The subprocess identity could not be established after spawn; no credential was released.');
          return;
        }
        if (cancelled || signal?.aborted || child.exitCode !== null || child.signalCode !== null) {
          discardSecret();
          closeCredentialPipe();
          if (cancelled || signal?.aborted) void stop();
          return;
        }
        if (secretInput) {
          // Persistence alone is not enough: the job may have been cancelled
          // or superseded while the durable write was in flight. Require the
          // owner to positively confirm the same process/attempt immediately
          // before releasing private stdin.
          const releaseAcknowledged = await onChildProcess(childRecord, 'credential_release');
          if (releaseAcknowledged !== true || cancelled || signal?.aborted
              || child.exitCode !== null || child.signalCode !== null) {
            throw new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'The setup attempt could not confirm current process ownership; no credential was released.');
          }
          if (!matchesProcessIdentity(childRecord, identityResolver(childRecord.pid, childRecord.nonce))) {
            discardSecret();
            closeCredentialPipe();
            void markTerminationUncertain('The process identity changed before credential release; no credential was sent.');
            return;
          }
          child.stdin.once('error', () => {});
          child.stdin.end(secretInput, () => secretInput.fill(0));
        }
      }).catch(() => {
        ownershipWriteFailed = true;
        discardSecret();
        closeCredentialPipe();
        void stop();
      });
    });
    child.once('close', async (code, childSignal) => {
      if (settled) return;
      settled = true;
      discardSecret();
      signal?.removeEventListener('abort', onAbort);
      await ownershipUpdate.catch(() => { ownershipWriteFailed = true; });
      let processGroupSafe = true;
      if (!spawnError && childRecord) {
        processGroupSafe = await waitForProcessGroupExit(child.pid, cancelled ? graceMs : 100);
        if (!processGroupSafe && matchesProcessIdentity(childRecord, identityResolver(child.pid, childRecord.nonce))) {
          try { signalProcessGroup(child.pid, 'SIGKILL'); } catch {}
          processGroupSafe = await waitForProcessGroupExit(child.pid, graceMs);
        }
      }
      if (killTimer) clearTimeout(killTimer);
      if (uncertainTimer) clearTimeout(uncertainTimer);
      if (processGroupSafe) {
        try { await onChildProcess?.({ ...childRecord, termination_status: 'termination_confirmed', clear: true }); }
        catch { ownershipWriteFailed = true; }
      } else if (childRecord) {
        try { await onChildProcess?.({ ...childRecord, termination_status: 'termination_uncertain',
          diagnostic: 'Process group exit could not be confirmed; retry is blocked.' }); }
        catch { ownershipWriteFailed = true; }
      } else if (spawnError) {
        try { await onChildProcess?.({ clear: true, termination_status: 'termination_confirmed' }); } catch {}
      }
      if (cancelled || signal?.aborted) {
        if (!processGroupSafe) {
          reject(new BootstrapError('BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'The bootstrap process group did not stop conclusively; ownership is retained and retry is blocked to protect managed files.'));
          return;
        }
        reject(new BootstrapError('BOOTSTRAP_CANCELLED', 'Bootstrap was cancelled after its owned process stopped.'));
      } else if (spawnError) {
        reject(new BootstrapError('BOOTSTRAP_PROCESS_START_FAILED', 'Could not start a trusted bootstrap operation.', { cause: spawnError }));
      } else if (!processGroupSafe) {
        reject(new BootstrapError('BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'A trusted bootstrap command left possible child writers; ownership is retained and installation is blocked.'));
      } else if (ownershipWriteFailed) {
        reject(new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'Could not persist subprocess ownership state; inspect managed bootstrap state before retrying.'));
      } else {
        resolve({ code, signal: childSignal,
          stdout: Buffer.concat(collected.stdout).toString('utf8'),
          stderr: Buffer.concat(collected.stderr).toString('utf8') });
      }
    });
  });
}

async function hashFile(file) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink()) throw new BootstrapError('BOOTSTRAP_FILE_UNSAFE', 'A bootstrap artifact path is not a regular file.');
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return { size: info.size, sha256: hash.digest('hex') };
}

function validateArtifactDigest(artifact, result) {
  if (result.size !== artifact.size_bytes || result.sha256 !== artifact.sha256) {
    throw new BootstrapError('BOOTSTRAP_INTEGRITY_FAILED', 'A downloaded bootstrap artifact did not match its trusted size and SHA-256.');
  }
}

function checkDisk(directory, requiredBytes, availableBytes = null) {
  let available = availableBytes;
  if (available === null) {
    try {
      const stat = fs.statfsSync(directory);
      available = stat.bavail * stat.bsize;
    } catch (error) {
      throw new BootstrapError('DISK_CAPACITY_UNKNOWN', 'Could not determine free space for the managed bootstrap filesystem; no download was started.', { cause: error });
    }
  }
  if (!Number.isFinite(available) || available < requiredBytes) {
    const mib = (bytes) => `${Math.ceil(bytes / (1024 * 1024))} MiB`;
    throw new BootstrapError('INSUFFICIENT_DISK_SPACE', `Bootstrap needs about ${mib(requiredBytes)} free; this filesystem has ${mib(Math.max(0, available || 0))}. Free space or move the managed SpeechCraft data directory to a larger supported filesystem.`);
  }
  return available;
}

export async function fetchArtifact(url, destination, expected, { signal, onProgress, fetchImpl = fetch,
  maxBytes = MAX_UV_ARCHIVE_BYTES } = {}) {
  let parsed;
  try { parsed = new URL(url); } catch { throw new BootstrapError('BOOTSTRAP_SOURCE_INVALID', 'The trusted bootstrap manifest contains an invalid source.'); }
  let response;
  try { response = await fetchImpl(parsed, { redirect: 'follow', signal }); }
  catch (error) {
    if (signal?.aborted || error?.name === 'AbortError') throw new BootstrapError('BOOTSTRAP_CANCELLED', 'Bootstrap was cancelled.');
    throw new BootstrapError('BOOTSTRAP_NETWORK_FAILED', 'Could not reach the trusted bootstrap download source. Check the network connection and retry.', { cause: error });
  }
  let finalUrl;
  try { finalUrl = new URL(response.url || parsed); } catch { finalUrl = parsed; }
  if (!['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'].includes(finalUrl.hostname)
      && finalUrl.hostname !== '127.0.0.1' && finalUrl.hostname !== 'localhost') {
    throw new BootstrapError('BOOTSTRAP_SOURCE_REJECTED', 'The trusted bootstrap source redirected to an unapproved host.');
  }
  if (!response.ok || !response.body) throw new BootstrapError('BOOTSTRAP_DOWNLOAD_FAILED', `A trusted bootstrap download failed with HTTP ${response.status}.`);
  const announcedRaw = response.headers.get('content-length');
  const announced = announcedRaw && /^\d+$/.test(announcedRaw) ? Number(announcedRaw) : null;
  if (announced !== null && announced > maxBytes) throw new BootstrapError('BOOTSTRAP_SIZE_REJECTED', 'A bootstrap download exceeded its allowed transfer size.');
  let file;
  try { file = await fs.promises.open(destination, 'w', 0o600); }
  catch (error) {
    if (error.code === 'ENOSPC') throw new BootstrapError('INSUFFICIENT_DISK_SPACE', 'The managed filesystem ran out of space before the bootstrap download could be written. Free space and retry.', { cause: error });
    throw new BootstrapError('BOOTSTRAP_WRITE_FAILED', 'Could not write the bootstrap artifact into controlled staging.', { cause: error });
  }
  const hash = createHash('sha256');
  let bytes = 0;
  try {
    const reader = response.body.getReader();
    while (true) {
      if (signal?.aborted) throw new BootstrapError('BOOTSTRAP_CANCELLED', 'Bootstrap was cancelled.');
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxBytes || bytes > expected.size_bytes) throw new BootstrapError('BOOTSTRAP_SIZE_REJECTED', 'A bootstrap download exceeded its trusted size.');
      hash.update(value);
      let offset = 0;
      while (offset < value.byteLength) {
        const { bytesWritten } = await file.write(value, offset, value.byteLength - offset);
        offset += bytesWritten;
      }
      onProgress?.({ phase: 'downloading', current: bytes, total: announced === expected.size_bytes ? expected.size_bytes : null,
        percent: announced === expected.size_bytes ? Math.floor(bytes * 100 / expected.size_bytes) : null,
        message: `Downloading pinned uv artifact (${bytes} bytes received).` });
    }
    await file.sync();
  } catch (error) {
    try { await response.body.cancel(); } catch {}
    if (error.code === 'ENOSPC') throw new BootstrapError('INSUFFICIENT_DISK_SPACE', 'The managed filesystem ran out of space during the bootstrap download. Free space and retry.', { cause: error });
    if (error instanceof BootstrapError) throw error;
    if (signal?.aborted || error?.name === 'AbortError') throw new BootstrapError('BOOTSTRAP_CANCELLED', 'Bootstrap was cancelled.');
    throw new BootstrapError('BOOTSTRAP_DOWNLOAD_INTERRUPTED', 'The bootstrap transfer ended unexpectedly; the incomplete staging file will be discarded on retry.', { cause: error });
  } finally { await file.close(); }
  const result = { size: bytes, sha256: hash.digest('hex') };
  validateArtifactDigest(expected, result);
  if (announced !== null && announced !== bytes) throw new BootstrapError('BOOTSTRAP_DOWNLOAD_TRUNCATED', 'The bootstrap server returned a different number of bytes than it announced.');
  return result;
}

function safeProgress(phase, message, current = null, total = null) {
  return { phase, current, total, percent: current !== null && total !== null && total > 0
    ? Math.floor(current * 100 / total) : null, message };
}

function parseProbe(result, kind, errorCode) {
  if (result.code !== 0) throw new BootstrapError(errorCode, 'A managed bootstrap component did not pass its runtime validation.');
  const line = result.stdout.split(/\r?\n/).find((value) => new RegExp(`"kind"\\s*:\\s*"${kind}"`).test(value));
  if (!line) throw new BootstrapError(errorCode, 'A managed bootstrap validation returned an unexpected response.');
  try { return JSON.parse(line); }
  catch (error) { throw new BootstrapError(errorCode, 'A managed bootstrap validation returned malformed data.', { cause: error }); }
}

function inspectDirectoryOrMissing(directory) {
  try {
    const info = fs.lstatSync(directory);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'A versioned bootstrap installation path is unsafe.');
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

function trustedFileMatches(file, expected) {
  if (!/^[a-f0-9]{64}$/.test(expected || '')) return false;
  try {
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.isSymbolicLink()) return false;
    return createHash('sha256').update(fs.readFileSync(file)).digest('hex') === expected;
  } catch { return false; }
}

export class BootstrapTasks {
  constructor({ dataDirectory, wheelPath = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL,
    wheelSha256 = process.env.SPEECHCRAFT_MODEL_MANAGER_WHEEL_SHA256,
    bootstrapManifest = manifest, requirementsPath = REQUIREMENTS, fetchImpl = fetch,
    availableBytes = null, processRunner = runOwnedProcess, runtimeRecipe = runtimeManifest,
    runtimeProbeAudio = process.env.SPEECHCRAFT_RUNTIME_PROBE_AUDIO || null,
    applicationRoot = fileURLToPath(new URL('../../', import.meta.url)) } = {}) {
    if (!dataDirectory) throw new BootstrapError('DATA_DIRECTORY_REQUIRED', 'A SpeechCraft-managed data directory is required for bootstrap.');
    this.dataDirectory = fs.realpathSync(dataDirectory);
    this.wheelPath = wheelPath;
    this.wheelSha256 = wheelSha256;
    this.manifest = bootstrapManifest;
    this.runtimeRecipe = runtimeRecipe;
    this.runtimeProbeAudio = runtimeProbeAudio;
    this.requirementsPath = requirementsPath;
    this.fetchImpl = fetchImpl;
    this.availableBytesOverride = availableBytes;
    this.processRunner = processRunner;
    this.applicationRoot = path.resolve(applicationRoot);
    this.environment = null;
    this.paths = null;
    this.stages = BOOTSTRAP_STAGES;
  }

  taskId(stage) { return `bootstrap:${stage}`; }
  initialProgress(stage, message) { return safeProgress(stage === 'inspect_system' ? 'inspecting' : 'queued', message); }
  completionProgress() { return safeProgress('complete', 'The configured setup plan finished. Component blockers and runtime readiness are reported separately; SpeechCraft is not production-ready.'); }
  initialBootstrapStatus() { return {
    profile: null, wsl: false, data_directory: this.dataDirectory,
    available_bytes: null, uv_verified: false, python_verified: false,
    model_manager_verified: false, bootstrap_ready: false, production_ready: false,
    components: initialComponents(), runtime_profile: null,
    model_root: path.join(this.dataDirectory, 'models'),
    hf_cache_root: path.join(this.dataDirectory, 'cache', 'bootstrap', 'huggingface'),
  }; }

  emit(context, progress) { context.onProgress?.(progress); }
  status(context, patch) {
    context.componentStatus = { ...context.componentStatus, ...patch,
      production_ready: patch.production_ready === true };
    context.onComponentStatus?.(structuredClone(context.componentStatus));
  }

  async recoverInterrupted(store) {
    const job = store.snapshot().job;
    if (!['bootstrap', 'runtime'].includes(job?.mode)) return;
    const status = this.initialBootstrapStatus();
    const priorStatus = job.bootstrap_status || {};
    const attemptId = job.attempt_id;
    try {
      this.environment = inspectHost();
      this.paths = this.getPaths(this.environment.profile, { allowMissingManager: true });
      const signal = new AbortController().signal;
      const context = { attemptId, signal, componentStatus: status,
        onComponentStatus: (value) => store.commit((state) => {
          if (state.job?.attempt_id === attemptId) state.job.bootstrap_status = value;
        }, 'bootstrap_component_revalidated'),
        onChildProcess: (value) => store.commit((state) => {
          if (state.job?.attempt_id === attemptId) state.job.child_process = value;
        }, value ? 'bootstrap_child_started' : 'bootstrap_child_stopped'),
        onProgress: () => {},
      };
      status.profile = this.environment.profile;
      status.wsl = this.environment.wsl;
      status.runtime_profile = structuredClone(priorStatus.runtime_profile || null);
      status.host_details = structuredClone(priorStatus.host_details || null);
      const uvVerified = await this.verifyUv(context);
      this.status(context, { uv_verified: uvVerified });
      if (uvVerified) {
        try {
          const python = await this.findPython(context);
          await this.verifyPython(python, context);
          this.status(context, { python_verified: true, python_version: this.manifest.python.version,
            python_executable: python });
          try {
            const manager = await this.findInstalledModelManager(context);
            this.paths.environmentRoot = manager.environmentRoot;
            this.status(context, { model_manager_verified: true, manager_version: manager.version });
            this.setComponent(context, 'model_manager', { state: 'verified', verification: 'installed_wheel_identity_catalog_and_cli',
              last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: manager.environmentRoot,
              wheel_sha256: manager.wheel_sha256 });
          } catch (error) {
            this.setComponent(context, 'model_manager', { state: 'blocked', path: null,
              failure_code: error.code || 'MODEL_MANAGER_ENVIRONMENT_UNAVAILABLE',
              message: error instanceof BootstrapError ? error.message.slice(0, 320)
                : 'The installed model-manager environment could not be verified; trusted repair inputs are required.' });
          }
        } catch {}
      }
      if (job.mode === 'runtime' && context.componentStatus.model_manager_verified) {
        try {
          const backendValid = await this.validateBackendEnvironment(context);
          this.setComponent(context, 'backend_environment', backendValid
            ? { state: 'verified', verification: 'lock_identity_and_import_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: this.backendEnvironmentPath() }
            : { state: 'pending', verification: 'revalidation_failed', failure_code: 'BACKEND_ENVIRONMENT_INVALID', message: 'The managed backend environment must be repaired before use.' });
        } catch (error) {
          this.setComponent(context, 'backend_environment', { state: 'pending', verification: 'revalidation_failed', failure_code: error.code || 'BACKEND_ENVIRONMENT_INVALID', message: 'The managed backend environment must be repaired before use.' });
        }
        const runtime = selectRuntimeProfile(this.environment);
        for (const [role, component] of [['speech_detection', 'silero'], ['asr', 'whisper'], ['speaker_purity', 'titanet'], ['diarization', 'pyannote']]) {
          try {
            const managerPython = path.join(this.paths.environmentRoot, 'bin', 'python');
            const result = await this.runProcess(managerPython, ['-I', '-m', 'speechcraft_models', 'status', role,
              '--platform', runtime.modelPlatform, '--root', path.join(this.dataDirectory, 'models')], context, { allowFailure: true });
            const report = JSON.parse(result.stdout);
            const installed = role === 'speaker_purity'
              ? report.integrity_verified === true && report.complete === true
              : report.install_ready === true && report.integrity_verified === true
                && (role !== 'diarization' || report.inventory_exact === true);
            const modelIdentity = component === 'pyannote' ? 'pyannote/speaker-diarization-community-1@3533c8cf8e369892e6b79ff1bf80f7b0286a54ee'
              : component === 'titanet' ? 'titanet-large-onnx-fp32/experimental' : undefined;
            this.setComponent(context, component, installed
              ? { state: 'files_verified', identity: modelIdentity,
                verification: role === 'diarization' ? 'complete_manifest_size_sha256_and_exact_tree'
                  : role === 'speaker_purity' ? 'experimental_manifest_size_and_sha256' : 'manifest_size_and_sha256',
                last_verified_at: new Date().toISOString(), failure_code: null,
                message: component === 'titanet' ? this.runtimeRecipe.models.blocked_roles.speaker_purity
                  : role === 'diarization' ? 'Community-1 files are verified; offline inference must be revalidated.' : 'Files are verified; inference readiness is not established.',
                runtime_ready: false, release_approved: role === 'speaker_purity' ? false : undefined,
                release_blockers: role === 'speaker_purity' ? report.release_blockers : undefined,
                path: report.root, report }
              : { state: role === 'diarization' ? 'waiting_for_token' : role === 'speaker_purity' ? 'blocked'
                  : ['missing', 'partial', 'corrupt', 'error', 'unverified'].includes(report.status) ? report.status : 'error',
                verification: 'manifest_check', failure_code: role === 'speaker_purity' && report.status === 'corrupt' ? 'TITANET_ARTIFACT_CORRUPT'
                  : role === 'diarization' && job.state !== 'waiting_for_user'
                  ? 'AUTHORIZATION_REQUIRED_AFTER_RESTART' : role === 'diarization' ? null : 'MODEL_FILES_NOT_VERIFIED',
                message: role === 'speaker_purity' && report.status === 'corrupt'
                  ? 'The experimental TitaNet artifact failed its trusted hash check; repair is required. Provenance remains unresolved.'
                  : role === 'speaker_purity' ? 'No complete, hash-verified TitaNet experimental artifact was found. Provenance and release approval remain unresolved.'
                  : role === 'diarization' && job.state !== 'waiting_for_user'
                  ? 'The controller restarted before gated acquisition completed. Its token was discarded; submit a read token to resume.'
                  : role === 'diarization' ? 'Community-1 is not completely verified. Submit a read token to acquire the pinned local snapshot.'
                    : 'Model files need repair before use.', runtime_ready: false,
                ...(role === 'speaker_purity' ? { release_approved: false } : {}), report });
          } catch {
            this.setComponent(context, component, { state: 'pending', verification: 'revalidation_failed', failure_code: 'MODEL_STATUS_UNAVAILABLE', message: 'Model files must be rechecked before use.', runtime_ready: false });
          }
        }
      }
      this.status(context, { bootstrap_ready: Boolean(context.componentStatus.uv_verified
        && context.componentStatus.python_verified && context.componentStatus.model_manager_verified),
      production_ready: false });
    } catch {
      store.commit((state) => { if (state.job?.attempt_id === attemptId) state.job.bootstrap_status = status; }, 'bootstrap_status_revalidation_failed');
    }
  }

  getPaths(profile = this.environment?.profile || 'linux-x86_64', { allowMissingManager = false, managerWheelSha256 = this.wheelSha256 } = {}) {
    const artifact = this.manifest.uv.artifacts[profile];
    if (!artifact) throw new BootstrapError('BOOTSTRAP_ARTIFACT_UNRESOLVED', `No bootstrap artifacts are cataloged for ${profile}.`);
    const python = this.manifest.python.supported_profiles[profile];
    if (!python || !/^[a-f0-9]{64}$/.test(python.sha256)) {
      throw new BootstrapError('BOOTSTRAP_ARTIFACT_UNRESOLVED', `No trusted managed Python distribution is cataloged for ${profile}.`);
    }
    const version = this.manifest.python.version;
    const build = this.manifest.python.build;
    const normalized = profile;
    if (!/^[a-f0-9]{64}$/.test(managerWheelSha256 || '') && !allowMissingManager) {
      throw new BootstrapError('MODEL_MANAGER_WHEEL_REQUIRED', 'Real bootstrap requires the exact locally shipped model-manager wheel SHA-256 from trusted launcher configuration.');
    }
    if (!fs.existsSync(this.requirementsPath) || fs.lstatSync(this.requirementsPath).isSymbolicLink()) {
      throw new BootstrapError('DEPENDENCY_LOCK_MISSING', 'The hash-locked model-manager dependencies are unavailable in the application package.');
    }
    const lockSha256 = createHash('sha256').update(fs.readFileSync(this.requirementsPath)).digest('hex');
    if (lockSha256 !== this.manifest.model_manager.requirements_sha256) {
      throw new BootstrapError('DEPENDENCY_LOCK_INTEGRITY_FAILED', 'The model-manager dependency lock does not match the trusted bootstrap manifest.');
    }
    const uvTarget = path.join(this.dataDirectory, 'runtimes', 'uv', this.manifest.uv.version, normalized);
    const pythonRoot = path.join(this.dataDirectory, 'runtimes', `cpython-${version}+${build}`, normalized);
    const environments = /^[a-f0-9]{64}$/.test(managerWheelSha256 || '')
      ? path.join(this.dataDirectory, 'environments', 'speechcraft-model-manager',
        `${this.manifest.model_manager.version || '0.1.0'}-py${version}-${normalized}-${managerWheelSha256.slice(0, 12)}-${lockSha256.slice(0, 12)}`)
      : null;
    const cacheRoot = path.join(this.dataDirectory, 'cache', 'bootstrap');
    const stagingRoot = path.join(this.dataDirectory, 'staging', 'bootstrap');
    const quarantineRoot = path.join(this.dataDirectory, 'quarantine', 'bootstrap');
    const dirs = [uvTarget, pythonRoot, ...(environments ? [environments] : []), cacheRoot, path.join(cacheRoot, 'uv'),
      path.join(cacheRoot, 'huggingface', 'hub'), path.join(cacheRoot, 'huggingface', 'xet'),
      path.join(cacheRoot, 'tmp'), path.join(cacheRoot, 'home'), stagingRoot, quarantineRoot];
    const resolved = validateManagedStorage(this.environment || { wsl: false }, [this.dataDirectory, ...dirs]);
    const map = new Map(dirs.map((dir, index) => [dir, resolved[index + 1]]));
    return {
      uvTarget: map.get(uvTarget), uvExecutable: path.join(map.get(uvTarget), 'uv'),
      pythonRoot: map.get(pythonRoot), environmentRoot: environments ? map.get(environments) : null,
      cacheRoot: map.get(cacheRoot), uvCache: map.get(path.join(cacheRoot, 'uv')),
      hfHome: path.join(map.get(cacheRoot), 'huggingface'),
      hfHub: map.get(path.join(cacheRoot, 'huggingface', 'hub')),
      hfXet: map.get(path.join(cacheRoot, 'huggingface', 'xet')),
      temporary: map.get(path.join(cacheRoot, 'tmp')), home: map.get(path.join(cacheRoot, 'home')),
      stagingRoot: map.get(stagingRoot), quarantineRoot: map.get(quarantineRoot),
    };
  }

  makeEnvironment(paths) {
    // Pass only host variables needed for executable lookup, certificates, and
    // explicit network proxies. Caller UV/PIP/HF settings cannot redirect
    // package sources or managed caches.
    const env = {};
    for (const key of ['PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
      'HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY', 'NO_PROXY', 'https_proxy',
      'http_proxy', 'all_proxy', 'no_proxy', 'SYSTEMROOT', 'WINDIR']) {
      if (process.env[key] !== undefined) env[key] = process.env[key];
    }
    Object.assign(env, {
      HOME: paths.home,
      XDG_CACHE_HOME: path.join(paths.home, '.cache'),
      TMPDIR: paths.temporary,
      TEMP: paths.temporary,
      TMP: paths.temporary,
      UV_NO_CONFIG: '1',
      UV_NO_PROGRESS: '1',
      UV_COLOR: 'never',
      UV_CACHE_DIR: paths.uvCache,
      UV_PYTHON_INSTALL_DIR: paths.pythonRoot,
      UV_PYTHON_INSTALL_BIN: '0',
      UV_LINK_MODE: 'copy',
      UV_DEFAULT_INDEX: 'https://pypi.org/simple',
      UV_INDEX_STRATEGY: 'first-index',
      PYTHONNOUSERSITE: '1',
      PIP_CONFIG_FILE: process.platform === 'win32' ? 'NUL' : '/dev/null',
      HF_HOME: paths.hfHome,
      HUGGINGFACE_HUB_CACHE: paths.hfHub,
      HF_XET_CACHE: paths.hfXet,
    });
    return env;
  }

  async runProcess(executable, args, context, { cwd = this.dataDirectory, env = this.makeEnvironment(this.paths), onOutput,
    allowFailure = false, stdinData } = {}) {
    if (this.processRunner === runOwnedProcess && typeof context.onChildProcess !== 'function') {
      throw new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'A trusted subprocess requires a durable setup ownership callback.');
    }
    const childCallback = (record, phase) => {
      if (!context.onChildProcess) return;
      if (record?.clear) return context.onChildProcess(null, 'termination_confirmed');
      return context.onChildProcess(record ? { ...record, attempt_id: context.attemptId } : null,
        phase || record?.termination_status || 'active');
    };
    // Persist a launch intent before spawning. A crash between spawn and the
    // subsequent PID write therefore fails closed instead of losing ownership.
    const intentAcknowledged = await context.onChildProcess?.({ attempt_id: context.attemptId, state: 'starting', pid: null,
      identity: null, termination_status: 'active', created_at: new Date().toISOString() }, 'active');
    if (intentAcknowledged === false) throw new BootstrapError('BOOTSTRAP_OWNERSHIP_PERSIST_FAILED', 'The setup attempt is no longer current; no subprocess was started.');
    const result = await this.processRunner(executable, args, { signal: context.signal, cwd, env, stdinData,
      onChildProcess: childCallback, onOutput });
    if (result.code !== 0 && !allowFailure) {
      throw new BootstrapError('BOOTSTRAP_COMMAND_FAILED', `A trusted bootstrap operation failed (exit code ${result.code ?? 'unknown'}).`);
    }
    return result;
  }

  currentFreeBytes() { return this.availableBytesOverride; }

  requireSpace(requiredBytes) {
    checkDisk(this.dataDirectory, requiredBytes, this.currentFreeBytes());
  }

  attemptDirectory(context) {
    const name = `attempt-${context.attemptId}`;
    const directory = path.join(this.paths.stagingRoot, name);
    ensureManagedDirectory(this.dataDirectory, directory);
    return directory;
  }

  cleanupOldAttempts(context) {
    const staging = this.paths.stagingRoot;
    for (const name of fs.readdirSync(staging)) {
      if (!/^attempt-[0-9a-f-]{36}$/i.test(name) || name === `attempt-${context.attemptId}`) continue;
      const target = path.join(staging, name);
      const info = fs.lstatSync(target);
      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'An unknown staging entry requires inspection; refusing cleanup.');
      }
      removeOwnedDirectory(this.dataDirectory, target);
    }
  }

  async run(stage, context) {
    context.componentStatus = context.bootstrapStatus || this.initialBootstrapStatus();
    if (stage === 'inspect_system') return this.inspectSystem(context);
    if (stage === 'install_uv') return this.ensureUv(context);
    if (stage === 'install_python') return this.ensurePython(context);
    if (stage === 'install_model_manager') return this.ensureModelManager(context);
    if (stage === 'install_backend_environment') return this.ensureBackendEnvironment(context);
    if (stage === 'install_worker_environment') return this.ensureWorkerEnvironment(context);
    if (stage === 'install_models') return this.installApprovedModels(context);
    if (stage === 'validate_runtime') return this.validateRuntime(context);
    if (stage === 'start_services') return this.markServicesBlocked(context);
    if (stage === 'verify_services') return this.verifyServices(context);
    if (stage === 'validate_bootstrap') return this.validateBootstrap(context);
    throw new BootstrapError('BOOTSTRAP_STAGE_UNKNOWN', 'The configured bootstrap stage is unknown.');
  }

  async inspectSystem(context) {
    this.emit(context, safeProgress('inspecting', 'Detecting supported OS, architecture, WSL, managed storage, and free space.'));
    try { this.environment = inspectHost(); }
    catch (error) { throw error instanceof BootstrapError ? error : new BootstrapError(error.code || 'PLATFORM_UNSUPPORTED', error.message); }
    this.paths = this.getPaths(this.environment.profile);
    this.cleanupOldAttempts(context);
    let nvidia = null;
    if (this.environment.os === 'linux') {
      try {
        // `cuda_version` is a display-only field on current nvidia-smi builds,
        // not a queryable GPU property. Keep this as candidate detection only;
        // actual CUDA selection requires runtime inference probes.
        const output = execFileSync('nvidia-smi', ['--query-gpu=name,driver_version', '--format=csv,noheader'], {
          encoding: 'utf8', timeout: 2_000, stdio: ['ignore', 'pipe', 'ignore'],
        }).trim().split(/\r?\n/).slice(0, 8);
        if (output.length && output[0]) nvidia = output.map((line) => line.slice(0, 240));
      } catch { nvidia = null; }
    }
    let available = this.currentFreeBytes();
    if (available === null) {
      try {
        const stat = fs.statfsSync(this.dataDirectory);
        available = stat.bavail * stat.bsize;
      } catch {
        throw new BootstrapError('DISK_CAPACITY_UNKNOWN', 'Could not determine free space for the managed bootstrap filesystem.');
      }
    }
    this.status(context, { profile: this.environment.profile, wsl: this.environment.wsl,
      available_bytes: available, data_directory: this.dataDirectory,
      runtime_profile: selectRuntimeProfile(this.environment, {
        gpuDetected: Boolean(nvidia?.length),
        cudaRecipeAvailable: this.runtimeRecipe.worker?.profiles?.['linux-x86_64-cuda']?.available === true
          && this.runtimeRecipe.worker?.profiles?.['linux-x86_64-cuda']?.validated === true,
        // Host visibility is only a candidate. CUDA becomes selectable after
        // managed PyTorch, CTranslate2, and required model probes all pass.
        cudaValidated: false,
      }),
      host_details: { cpu_model: os.cpus()[0]?.model?.slice(0, 160) || null,
        logical_cpu_count: os.cpus().length, nvidia_gpus: nvidia,
        cuda_detected: Boolean(nvidia?.length), cuda_candidate: Boolean(nvidia?.length), cuda_compatibility: 'not_assessed',
        cuda_runtime_validated: false },
      uv_verified: false, python_verified: false, model_manager_verified: false,
      bootstrap_ready: false, production_ready: false });
    this.emit(context, safeProgress('inspected', `Supported ${this.environment.profile} host and managed storage confirmed.`));
  }

  async quarantine(target, label) {
    if (!fs.existsSync(target)) return;
    const info = fs.lstatSync(target);
    if (info.isSymbolicLink() || (!info.isDirectory() && !info.isFile())) {
      throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'An invalid managed artifact has an unsafe filesystem type.');
    }
    const name = `${label}-${Date.now()}-${randomUUID()}`;
    const destination = path.join(this.paths.quarantineRoot, name);
    assertInside(this.dataDirectory, target);
    assertInside(this.dataDirectory, destination);
    fs.renameSync(target, destination);
  }

  async verifyUv(context) {
    const target = this.paths.uvExecutable;
    try {
      const info = fs.lstatSync(target);
      if (!info.isFile() || info.isSymbolicLink() || (process.platform !== 'win32' && (info.mode & 0o111) === 0)) return false;
    } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
    try {
      const fileIdentity = await hashFile(target);
      const trusted = this.manifest.uv.artifacts[this.environment.profile];
      if (fileIdentity.size !== trusted.executable_size_bytes || fileIdentity.sha256 !== trusted.executable_sha256) return false;
      const result = await this.runProcess(target, ['--version'], context);
      const expected = `uv ${this.manifest.uv.version}`;
      return result.stdout.trim().startsWith(expected);
    } catch (error) {
      if (error.code === 'BOOTSTRAP_CANCELLED') throw error;
      return false;
    }
  }

  async ensureUv(context) {
    if (!this.environment || !this.paths) throw new BootstrapError('SYSTEM_NOT_INSPECTED', 'Inspect the host before installing uv.');
    if (await this.verifyUv(context)) {
      this.status(context, { uv_verified: true });
      this.emit(context, safeProgress('verified', 'The pinned uv executable is already verified and was reused.'));
      return;
    }
    const artifact = this.manifest.uv.artifacts[this.environment.profile];
    this.requireSpace(artifact.size_bytes + artifact.executable_size_bytes
      + this.manifest.disk_space.safety_margin_bytes);
    const directory = this.attemptDirectory(context);
    const archive = path.join(directory, artifact.file);
    for (const target of [archive]) {
      if (fs.existsSync(target)) {
        const info = fs.lstatSync(target);
        if (info.isSymbolicLink() || !info.isFile()) throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'A uv staging artifact is not a regular file.');
        fs.unlinkSync(target);
      }
    }
    this.emit(context, safeProgress('downloading', 'Downloading the pinned Astral uv release artifact.'));
    await fetchArtifact(artifact.url, archive, artifact, { signal: context.signal,
      fetchImpl: this.fetchImpl, onProgress: (value) => this.emit(context, value) });
    const digest = await hashFile(archive);
    validateArtifactDigest(artifact, digest);
    this.emit(context, safeProgress('verifying', 'Verifying the official uv archive SHA-256 and safe archive inventory.'));
    const uvBytes = extractUvArchive(fs.readFileSync(archive), artifact);
    const installStage = path.join(directory, `uv-install-${randomUUID()}`);
    ensureManagedDirectory(this.dataDirectory, installStage);
    const executable = path.join(installStage, 'uv');
    if (uvBytes.byteLength !== artifact.executable_size_bytes
        || createHash('sha256').update(uvBytes).digest('hex') !== artifact.executable_sha256) {
      throw new BootstrapError('UV_EXECUTABLE_HASH_MISMATCH', 'The extracted uv executable did not match its trusted size and SHA-256.');
    }
    fs.writeFileSync(executable, uvBytes, { mode: 0o700, flag: 'wx' });
    fs.chmodSync(executable, 0o755);
    const version = await this.runProcess(executable, ['--version'], context, { cwd: installStage });
    if (!version.stdout.trim().startsWith(`uv ${this.manifest.uv.version}`)) {
      throw new BootstrapError('UV_VERSION_MISMATCH', 'The downloaded uv executable did not report the pinned version.');
    }
    await this.quarantine(this.paths.uvTarget, 'invalid-uv');
    fs.renameSync(installStage, this.paths.uvTarget);
    fs.chmodSync(this.paths.uvExecutable, 0o755);
    this.status(context, { uv_verified: true });
    this.emit(context, safeProgress('verified', 'The pinned uv executable passed its version and integrity checks.'));
  }

  async findPython(context) {
    const result = await this.runProcess(this.paths.uvExecutable,
      ['python', 'find', '--managed-python', '--no-project', '--resolve-links', this.manifest.python.version], context);
    const executable = result.stdout.trim().split(/\r?\n/).at(-1);
    if (!path.isAbsolute(executable)) throw new BootstrapError('PYTHON_PATH_INVALID', 'uv did not return an absolute managed Python path.');
    const resolved = fs.realpathSync(executable);
    assertInside(this.paths.pythonRoot, resolved);
    const info = fs.lstatSync(resolved);
    if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o111) === 0) {
      throw new BootstrapError('PYTHON_EXECUTABLE_INVALID', 'uv did not resolve a regular executable inside managed Python storage.');
    }
    return resolved;
  }

  async verifyPython(executable, context) {
    if (!executable) return null;
    const result = await this.runProcess(executable, ['-I', '-c', PYTHON_PROBE], context);
    const probe = parseProbe(result, 'SPEECHCRAFT_PYTHON_PROBE', 'PYTHON_VALIDATION_FAILED');
    if (probe.version !== this.manifest.python.version || probe.system !== (this.environment.profile.startsWith('macos') ? 'Darwin' : 'Linux')) {
      throw new BootstrapError('PYTHON_VERSION_MISMATCH', 'The managed interpreter reported a different version or operating system.');
    }
    const expectedMachine = this.environment.profile === 'macos-arm64' ? 'arm64' : 'x86_64';
    if (!['x86_64', 'amd64'].includes(probe.machine.toLowerCase()) && expectedMachine === 'x86_64') {
      throw new BootstrapError('PYTHON_ARCH_MISMATCH', 'The managed interpreter architecture does not match the host.');
    }
    if (expectedMachine === 'arm64' && !['arm64', 'aarch64'].includes(probe.machine.toLowerCase())) {
      throw new BootstrapError('PYTHON_ARCH_MISMATCH', 'The managed interpreter architecture does not match Apple Silicon.');
    }
    assertInside(this.paths.pythonRoot, probe.prefix);
    assertInside(this.paths.pythonRoot, probe.executable);
    return probe;
  }

  async ensurePython(context) {
    if (!await this.verifyUv(context)) throw new BootstrapError('UV_NOT_VERIFIED', 'Install or repair pinned uv before installing managed Python.');
    const profile = this.environment.profile;
    const pythonArtifact = this.manifest.python.supported_profiles[profile];
    let executable = null;
    try { executable = await this.findPython(context); }
    catch (error) {
      if (error.code === 'BOOTSTRAP_CANCELLED') throw error;
      if (error.code !== 'BOOTSTRAP_COMMAND_FAILED' && error.code !== 'ENOENT') throw error;
    }
    if (executable) {
      try {
        await this.verifyPython(executable, context);
        this.status(context, { python_verified: true });
        this.emit(context, safeProgress('verified', `Managed CPython ${this.manifest.python.version} is verified and was reused.`));
        return;
      } catch (error) {
        if (error.code === 'BOOTSTRAP_CANCELLED') throw error;
      }
    }
    this.requireSpace(pythonArtifact.size_bytes + this.manifest.disk_space.estimated_python_environment_bytes
      + this.manifest.disk_space.safety_margin_bytes);
    this.emit(context, safeProgress('downloading', `Installing managed CPython ${this.manifest.python.version} from uv's pinned python-build-standalone metadata.`));
    await this.quarantine(this.paths.pythonRoot, 'invalid-python');
    ensureManagedDirectory(this.dataDirectory, this.paths.pythonRoot);
    await this.runProcess(this.paths.uvExecutable, ['python', 'install', '--no-config', '--managed-python',
      '--no-bin', '--install-dir', this.paths.pythonRoot, '--cache-dir', this.paths.uvCache,
      this.manifest.python.version], context);
    executable = await this.findPython(context);
    await this.verifyPython(executable, context);
    this.status(context, { python_verified: true });
    this.emit(context, safeProgress('verified', `Managed CPython ${this.manifest.python.version} passed its interpreter probe.`));
  }

  managerWheelIdentity() {
    if (typeof this.wheelPath !== 'string' || !path.isAbsolute(this.wheelPath)
        || !/^[a-f0-9]{64}$/.test(this.wheelSha256 || '')) {
      throw new BootstrapError('MODEL_MANAGER_WHEEL_REQUIRED', 'Real bootstrap requires the exact locally shipped model-manager wheel path and SHA-256 from trusted launcher configuration.');
    }
    const info = fs.lstatSync(this.wheelPath);
    if (!info.isFile() || info.isSymbolicLink()) throw new BootstrapError('MODEL_MANAGER_WHEEL_UNSAFE', 'The configured model-manager wheel must be a regular non-symlink file.');
    const name = path.basename(this.wheelPath).toLowerCase();
    if (!name.startsWith('speechcraft_model_manager-0.1.0-') || !name.endsWith('.whl')) {
      throw new BootstrapError('MODEL_MANAGER_WHEEL_UNEXPECTED', 'The configured wheel is not the expected SpeechCraft model-manager release.');
    }
    return this.wheelPath;
  }

  async verifyModelManager(python, context) {
    const wheel = this.managerWheelIdentity();
    const identity = await hashFile(wheel);
    if (identity.sha256 !== this.wheelSha256) throw new BootstrapError('MODEL_MANAGER_WHEEL_HASH_MISMATCH', 'The model-manager wheel did not match its trusted SHA-256.');
    const result = await this.runProcess(python, ['-I', '-B', '-c', MANAGER_PROBE, wheel, this.wheelSha256], context, {
      env: { ...this.makeEnvironment(this.paths), SPEECHCRAFT_EXPECTED_MANAGER_PREFIX: this.paths.environmentRoot,
        SPEECHCRAFT_EXPECTED_MANAGED_PYTHON_ROOT: this.paths.pythonRoot },
    });
    return this.validateManagerProbe(parseProbe(result, 'SPEECHCRAFT_MODEL_MANAGER_PROBE', 'MODEL_MANAGER_PROBE_FAILED'), this.paths.environmentRoot);
  }

  validateManagerProbe(probe, expectedEnvironmentRoot) {
    if (probe.failure_code) {
      const messages = {
        MODEL_MANAGER_WHEEL_HASH_MISMATCH: 'The locally shipped model-manager wheel did not match its trusted SHA-256.',
        MODEL_MANAGER_WHEEL_INVALID: 'The trusted model-manager wheel has an invalid or unsafe archive structure.',
        MODEL_MANAGER_INSTALLED_FILES_MISMATCH: 'Installed model-manager files differ from the trusted wheel contents.',
        MODEL_MANAGER_PACKAGE_VERSION_MISMATCH: 'The installed model-manager package version does not match the supported release.',
        MODEL_MANAGER_ENVIRONMENT_PATH_MISMATCH: 'The model-manager virtual environment or managed Python path is invalid.',
        MODEL_MANAGER_MANAGED_PYTHON_PATH_MISMATCH: 'The model-manager virtual environment or managed Python path is invalid.',
        MODEL_MANAGER_CATALOG_INVALID: 'The installed model-manager catalog is invalid or missing required model roles.',
        UNEXPECTED_ML_DEPENDENCY: 'The lightweight model-manager environment contains an unexpected ML dependency.',
      };
      throw new BootstrapError(probe.failure_code, messages[probe.failure_code] || 'The installed model-manager package failed validation.');
    }
    if (probe.version !== (this.manifest.model_manager.version || '0.1.0')) {
      throw new BootstrapError('MODEL_MANAGER_PACKAGE_VERSION_MISMATCH', 'The installed model-manager package version does not match the supported release.');
    }
    if (probe.python !== this.manifest.python.version) {
      throw new BootstrapError('MODEL_MANAGER_MANAGED_PYTHON_VERSION_MISMATCH', 'The model-manager environment uses an unexpected managed Python version.');
    }
    const expectedPrefix = fs.realpathSync(expectedEnvironmentRoot);
    const managedPythonRoot = fs.realpathSync(this.paths.pythonRoot);
    const basePrefix = probe.base_prefix || '';
    if (probe.prefix !== expectedPrefix) {
      throw new BootstrapError('MODEL_MANAGER_ENVIRONMENT_PATH_MISMATCH', 'The model-manager virtual environment or managed Python path is invalid.');
    }
    if (basePrefix !== managedPythonRoot && !basePrefix.startsWith(`${managedPythonRoot}${path.sep}`)) {
      throw new BootstrapError('MODEL_MANAGER_MANAGED_PYTHON_PATH_MISMATCH', 'The model-manager virtual environment or managed Python path is invalid.');
    }
    if (!probe.catalog_valid || !Array.isArray(probe.roles)
        || !['diarization', 'asr', 'speech_detection', 'speaker_purity'].every((role) => probe.roles.includes(role))) {
      throw new BootstrapError('MODEL_MANAGER_CATALOG_INVALID', 'The installed model-manager catalog is invalid or missing required model roles.');
    }
    if (!Array.isArray(probe.heavy_dependencies) || probe.heavy_dependencies.length) {
      throw new BootstrapError('UNEXPECTED_ML_DEPENDENCY', 'The lightweight model-manager environment contains an unexpected ML dependency.');
    }
    return probe;
  }

  async findInstalledModelManager(context) {
    const wheel = this.managerWheelIdentity();
    const identity = await hashFile(wheel);
    if (identity.sha256 !== this.wheelSha256) throw new BootstrapError('MODEL_MANAGER_WHEEL_HASH_MISMATCH', 'The model-manager wheel did not match its trusted SHA-256.');
    const version = this.manifest.model_manager.version || '0.1.0';
    const pythonVersion = this.manifest.python.version;
    const lockSha = this.manifest.model_manager.requirements_sha256;
    if (!/^[a-f0-9]{64}$/.test(lockSha || '') || !fs.existsSync(this.requirementsPath)
        || createHash('sha256').update(fs.readFileSync(this.requirementsPath)).digest('hex') !== lockSha) {
      throw new BootstrapError('DEPENDENCY_LOCK_INTEGRITY_FAILED', 'The model-manager dependency lock is missing or differs from the trusted installation identity.');
    }
    const parent = path.join(this.dataDirectory, 'environments', 'speechcraft-model-manager');
    let entries;
    try {
      const info = fs.lstatSync(parent);
      if (info.isSymbolicLink() || !info.isDirectory()) throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'The managed model-manager environment root is unsafe.');
      entries = fs.readdirSync(parent, { withFileTypes: true });
    } catch (error) {
      if (error.code === 'ENOENT') throw new BootstrapError('MODEL_MANAGER_ENVIRONMENT_UNAVAILABLE', 'The installed model-manager environment is missing; use trusted repair inputs to reinstall it.');
      throw error;
    }
    const pattern = new RegExp(`^${version.replaceAll('.', '\\.')}\\-py${pythonVersion.replaceAll('.', '\\.')}\\-${this.environment.profile}\\-([a-f0-9]{12})\\-${lockSha.slice(0, 12)}$`);
    const candidates = entries.filter((entry) => entry.isDirectory() && !entry.isSymbolicLink() && pattern.test(entry.name));
    let validationFailure = null;
    for (const entry of candidates) {
      const environmentRoot = path.join(parent, entry.name);
      const python = path.join(environmentRoot, 'bin', 'python');
      try {
        const rootInfo = fs.lstatSync(environmentRoot);
        const binInfo = fs.lstatSync(path.join(environmentRoot, 'bin'));
        const info = fs.statSync(python);
        const resolvedEnvironment = fs.realpathSync(environmentRoot);
        const resolvedPython = fs.realpathSync(python);
        const resolvedManagedPython = fs.realpathSync(this.paths.pythonRoot);
        const trustedPythonPath = resolvedPython.startsWith(`${resolvedEnvironment}${path.sep}`)
          || resolvedPython.startsWith(`${resolvedManagedPython}${path.sep}`);
        if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink() || resolvedEnvironment !== environmentRoot
            || !binInfo.isDirectory() || binInfo.isSymbolicLink() || !info.isFile()
            || !trustedPythonPath || (info.mode & 0o111) === 0) continue;
        const result = await this.runProcess(python, ['-I', '-B', '-c', MANAGER_PROBE, wheel, this.wheelSha256], context, { allowFailure: true,
          env: { ...this.makeEnvironment(this.paths), SPEECHCRAFT_EXPECTED_MANAGER_PREFIX: environmentRoot,
            SPEECHCRAFT_EXPECTED_MANAGED_PYTHON_ROOT: this.paths.pythonRoot } });
        const probe = JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1));
        const wheelPrefix = entry.name.match(pattern)?.[1];
        if (result.code === 0 && probe.kind === 'SPEECHCRAFT_MODEL_MANAGER_PROBE') {
          try {
            const validated = this.validateManagerProbe(probe, environmentRoot);
            if (validated.wheel_sha256 !== this.wheelSha256 || validated.wheel_sha256.slice(0, 12) !== wheelPrefix) {
              throw new BootstrapError('MODEL_MANAGER_WHEEL_HASH_MISMATCH', 'The installed environment identity does not match the trusted model-manager wheel.');
            }
            return { ...validated, environmentRoot, python };
          } catch (error) { validationFailure = error; }
        }
      } catch (error) {
        if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
      }
    }
    if (validationFailure) throw validationFailure;
    throw new BootstrapError('MODEL_MANAGER_ENVIRONMENT_UNAVAILABLE', 'No installed model-manager environment passed its pinned package, wheel, lock, and catalog identity checks. Trusted repair inputs are required to reinstall it.');
  }

  async ensureModelManager(context) {
    if (!this.manifest.model_manager.dependency_lock) throw new BootstrapError('DEPENDENCY_LOCK_MISSING', 'The model-manager dependency lock is missing.');
    if (!fs.existsSync(this.requirementsPath) || fs.lstatSync(this.requirementsPath).isSymbolicLink()) {
      throw new BootstrapError('DEPENDENCY_LOCK_MISSING', 'The hash-locked model-manager dependencies are unavailable in the application package.');
    }
    const wheel = this.managerWheelIdentity();
    const identity = await hashFile(wheel);
    if (identity.sha256 !== this.wheelSha256) throw new BootstrapError('MODEL_MANAGER_WHEEL_HASH_MISMATCH', 'The model-manager wheel did not match its trusted SHA-256.');
    const managedPython = context.managedPython || await this.findPython(context);
    await this.verifyPython(managedPython, context);
    context.managedPython = managedPython;
    const target = this.paths.environmentRoot;
    if (inspectDirectoryOrMissing(target)) {
      const python = path.join(target, 'bin', 'python');
      try {
        const manager = await this.verifyModelManager(python, context);
        this.status(context, { model_manager_verified: true });
        this.setComponent(context, 'model_manager', { state: 'verified', verification: 'catalog_import_and_cli',
          last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target,
          wheel_sha256: manager.wheel_sha256, lock_sha256: this.manifest.model_manager.requirements_sha256 });
        this.emit(context, safeProgress('verified', 'The isolated model-manager environment is verified and was reused.'));
        return;
      } catch (error) {
        if (error.code === 'BOOTSTRAP_CANCELLED') throw error;
      }
      await this.quarantine(target, 'invalid-model-manager-env');
    }
    this.requireSpace(this.manifest.disk_space.estimated_model_manager_environment_and_cache_bytes
      + this.manifest.disk_space.safety_margin_bytes);
    this.emit(context, safeProgress('installing', 'Creating the versioned isolated model-manager Python environment.'));
    const env = this.makeEnvironment(this.paths);
    await this.runProcess(this.paths.uvExecutable, ['venv', '--no-config', '--no-project',
      '--python', managedPython, target], context, { env });
    const environmentPython = path.join(target, 'bin', 'python');
    await this.runProcess(this.paths.uvExecutable, ['pip', 'sync', '--no-config', '--require-hashes',
      '--only-binary', ':all:', '--python', environmentPython, this.requirementsPath], context, { env });
    await this.runProcess(this.paths.uvExecutable, ['pip', 'install', '--no-config', '--no-deps',
      '--python', environmentPython, wheel], context, { env });
    await this.verifyModelManager(environmentPython, context);
    this.status(context, { model_manager_verified: true });
    this.setComponent(context, 'model_manager', { state: 'verified', verification: 'catalog_import_and_cli',
      last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target,
      wheel_sha256: this.wheelSha256, lock_sha256: this.manifest.model_manager.requirements_sha256 });
    this.emit(context, safeProgress('verified', 'The isolated model-manager environment passed import, catalog, CLI, and dependency checks.'));
  }

  async validateBootstrap(context) {
    if (!await this.verifyUv(context)) throw new BootstrapError('UV_VALIDATION_FAILED', 'The managed uv executable did not pass its version check.');
    const python = await this.findPython(context);
    const pythonInfo = await this.verifyPython(python, context);
    context.managedPython = python;
    const manager = await this.verifyModelManager(path.join(this.paths.environmentRoot, 'bin', 'python'), context);
    const cli = await this.runProcess(path.join(this.paths.environmentRoot, 'bin', 'python'),
      ['-I', '-m', 'speechcraft_models', '--help'], context);
    if (!cli.stdout.includes('install')) throw new BootstrapError('MODEL_MANAGER_CLI_INVALID', 'The model-manager developer validation CLI did not return its help contract.');
    if (manager.heavy_dependencies.length) throw new BootstrapError('UNEXPECTED_ML_DEPENDENCY', 'The lightweight model-manager environment contains an ML inference dependency.');
    this.status(context, { uv_verified: true, python_verified: true, model_manager_verified: true,
      bootstrap_ready: true, production_ready: false, python_version: pythonInfo.version,
      python_executable: python, manager_version: manager.version });
    this.setComponent(context, 'uv', { state: 'verified', verification: 'sha256_and_version', last_verified_at: new Date().toISOString(), failure_code: null, message: null });
    this.setComponent(context, 'managed_python', { state: 'verified', verification: 'runtime_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null });
    this.setComponent(context, 'model_manager', { state: 'verified', verification: 'catalog_import_and_cli', last_verified_at: new Date().toISOString(), failure_code: null, message: null,
      path: this.paths.environmentRoot, wheel_sha256: this.wheelSha256, lock_sha256: this.manifest.model_manager.requirements_sha256 });
    const attemptDirectory = path.join(this.paths.stagingRoot, `attempt-${context.attemptId}`);
    removeOwnedDirectory(this.dataDirectory, attemptDirectory);
    this.emit(context, safeProgress('complete', 'Bootstrap components are verified; models and inference are not installed or ready.'));
  }

  setComponent(context, name, patch) {
    const current = context.componentStatus || this.initialBootstrapStatus();
    current.components ||= initialComponents();
    current.components[name] = { ...(current.components[name] || {}), ...patch };
    this.status(context, { components: current.components, production_ready: false });
  }

  getManagerPython() { return path.join(this.paths.environmentRoot, 'bin', 'python'); }

  backendEnvironmentPath() {
    return path.join(this.dataDirectory, 'environments', 'backend', `0.1.0-py${this.manifest.python.version}-${this.environment.profile}-${runtimeManifest.backend.lock_sha256.slice(0, 12)}`);
  }

  async validateBackendEnvironment(context, target = this.backendEnvironmentPath()) {
    const { project } = assertTrustedBackendRecipe(this.applicationRoot);
    const python = path.join(target, 'bin', 'python');
    const probe = String.raw`import importlib.util, json, os, sys
assert sys.version_info[:3] == (3,12,15), sys.version
assert importlib.util.find_spec('fastapi')
assert os.path.realpath(sys.prefix) == os.path.realpath(${JSON.stringify(target)})
base = os.path.realpath(sys.base_prefix); runtime = os.path.realpath(${JSON.stringify(this.paths.pythonRoot)})
assert base == runtime or base.startswith(runtime + os.sep)
sys.path.insert(0, ${JSON.stringify(project)})
assert importlib.util.find_spec('app.main')
import app.models, app.dataset_worker_client
print(json.dumps({'kind':'SPEECHCRAFT_BACKEND_ENV_PROBE','python':sys.executable,'version':'.'.join(map(str,sys.version_info[:3]))}))`;
    if (!inspectDirectoryOrMissing(target)) return false;
    try {
      const result = await this.runProcess(python, ['-I', '-c', probe], context, { cwd: project });
      parseProbe(result, 'SPEECHCRAFT_BACKEND_ENV_PROBE', 'BACKEND_ENVIRONMENT_INVALID');
      return true;
    } catch (error) {
      if (error.code === 'BOOTSTRAP_CANCELLED') throw error;
      return false;
    }
  }

  async ensureBackendEnvironment(context) {
    const selection = selectRuntimeProfile(this.environment);
    if (!runtimeManifest.backend.profiles.includes(selection.profile)) {
      this.setComponent(context, 'backend_environment', { state: 'blocked', failure_code: 'runtime_profile_unavailable', message: `No trusted backend recipe is available for ${selection.profile}.` });
      return;
    }
    const { project, lock } = assertTrustedBackendRecipe(this.applicationRoot);
    const lockDigest = createHash('sha256').update(fs.readFileSync(lock)).digest('hex');
    if (lockDigest !== runtimeManifest.backend.lock_sha256) throw new BootstrapError('BACKEND_RECIPE_INTEGRITY_FAILED', 'The backend dependency lock does not match the trusted runtime manifest.');
    const target = this.backendEnvironmentPath();
    if (await this.validateBackendEnvironment(context, target)) {
      this.setComponent(context, 'backend_environment', { state: 'verified', verification: 'lock_identity_and_import_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target });
      return;
    }
    if (inspectDirectoryOrMissing(target)) await this.quarantine(target, 'invalid-backend-env');
    ensureManagedDirectory(this.dataDirectory, target);
    this.requireSpace(runtimeManifest.disk_space.backend_environment_estimate_bytes + runtimeManifest.disk_space.safety_margin_bytes);
    const env = { ...this.makeEnvironment(this.paths), UV_PROJECT_ENVIRONMENT: target };
    this.emit(context, safeProgress('installing', 'Installing the backend from its repository lockfile into a versioned managed environment.'));
    await this.runProcess(this.paths.uvExecutable, ['sync', '--locked', '--no-dev', '--no-install-project', '--python', context.managedPython || await this.findPython(context), '--project', project], context, { cwd: project, env });
    if (!await this.validateBackendEnvironment(context, target)) throw new BootstrapError('BACKEND_ENVIRONMENT_INVALID', 'The installed backend did not pass its managed-interpreter import probe.');
    this.setComponent(context, 'backend_environment', { state: 'verified', verification: 'lock_identity_and_import_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target });
  }

  workerEnvironmentPath(recipe, profile = selectRuntimeProfile(this.environment).workerProfile) {
    return path.join(this.dataDirectory, 'environments', 'worker', `v${recipe.environment_version}-${recipe.lock_sha256.slice(0, 16)}-${profile}`);
  }

  async ensureWorkerEnvironment(context) {
    const selection = selectRuntimeProfile(this.environment);
    const recipe = this.runtimeRecipe.worker?.profiles?.[selection.workerProfile];
    await this.installManagedFfmpeg(context);
    const recipeInstallable = recipe?.available === true && (recipe.validated === true || recipe.lock_verified === true);
    if (!recipeInstallable) {
      const reason = recipe?.reason || `No trusted, validated worker recipe is available for ${selection.workerProfile}.`;
      this.setComponent(context, 'worker_environment', { state: 'blocked', failure_code: 'runtime_profile_unavailable', message: reason });
      this.emit(context, safeProgress('blocked', reason));
      return;
    }
    const digestPattern = /^[a-f0-9]{64}$/;
    if (!digestPattern.test(recipe.lock_sha256 || '') || !digestPattern.test(recipe.project_manifest_sha256 || '')
        || !Number.isSafeInteger(recipe.environment_version) || recipe.environment_version < 1
        || recipe.python_version !== this.manifest.python.version
        || !Array.isArray(recipe.validation_modules) || recipe.validation_modules.some((name) => !/^[A-Za-z_][A-Za-z0-9_.]*$/.test(name))) {
      throw new BootstrapError('WORKER_RECIPE_INVALID', 'The trusted worker recipe is incomplete or malformed.');
    }
    const project = path.resolve(this.applicationRoot, recipe.project || this.runtimeRecipe.worker.project);
    const relativeProject = path.relative(this.applicationRoot, project);
    if (relativeProject === '..' || relativeProject.startsWith(`..${path.sep}`) || path.isAbsolute(relativeProject)) {
      throw new BootstrapError('WORKER_RECIPE_INVALID', 'The worker project path escapes the application package.');
    }
    const lock = path.resolve(project, recipe.lockfile || this.runtimeRecipe.worker.lockfile.split('/').at(-1));
    const lockRelative = path.relative(project, lock);
    if (lockRelative === '..' || lockRelative.startsWith(`..${path.sep}`) || path.isAbsolute(lockRelative)) {
      throw new BootstrapError('WORKER_RECIPE_INVALID', 'The worker lockfile path escapes its trusted project.');
    }
    const projectManifest = path.join(project, 'pyproject.toml');
    if (!trustedFileMatches(lock, recipe.lock_sha256) || !trustedFileMatches(projectManifest, recipe.project_manifest_sha256)) {
      throw new BootstrapError('WORKER_RECIPE_INTEGRITY_FAILED', 'The worker lock or project manifest differs from its trusted recipe.');
    }
    const workerSourceManifest = path.join(this.applicationRoot, this.runtimeRecipe.worker.project, 'pyproject.toml');
    if (this.runtimeRecipe.worker.source_manifest_sha256
        && !trustedFileMatches(workerSourceManifest, this.runtimeRecipe.worker.source_manifest_sha256)) {
      throw new BootstrapError('WORKER_SOURCE_INTEGRITY_FAILED', 'The worker source package manifest differs from its trusted runtime recipe.');
    }
    const target = this.workerEnvironmentPath(recipe);
    const python = path.join(target, 'bin', 'python');
    const machines = expectedWorkerMachines(selection.workerProfile);
    const probe = `import importlib.util,json,os,platform,sys\nassert sys.version.split()[0]==${JSON.stringify(recipe.python_version)},sys.version\nassert os.path.realpath(sys.prefix)==os.path.realpath(${JSON.stringify(target)})\nbase=os.path.realpath(sys.base_prefix); runtime=os.path.realpath(${JSON.stringify(this.paths.pythonRoot)})\nassert base==runtime or base.startswith(runtime+os.sep),base\nassert platform.machine().lower() in ${JSON.stringify(machines)},platform.machine()\nmodules=${JSON.stringify(recipe.validation_modules)}\nmissing=[name for name in modules if importlib.util.find_spec(name) is None]\nassert not missing,missing\nprint(json.dumps({'kind':'SPEECHCRAFT_WORKER_ENV_PROBE','profile':${JSON.stringify(selection.workerProfile)},'version':sys.version.split()[0],'missing':missing,'python':sys.executable}))`;
    if (inspectDirectoryOrMissing(target)) {
      try {
        const result = await this.runProcess(python, ['-I', '-c', probe], context, { cwd: project });
        parseProbe(result, 'SPEECHCRAFT_WORKER_ENV_PROBE', 'WORKER_ENVIRONMENT_INVALID');
        this.setComponent(context, 'worker_environment', { state: 'verified', identity: `${this.runtimeRecipe.worker.distribution}/v${recipe.environment_version}/${selection.workerProfile}`,
          verification: 'trusted_lock_and_import_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target });
        return;
      } catch (error) {
        if (error.code === 'BOOTSTRAP_CANCELLED' || error.code === 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN') throw error;
        await this.quarantine(target, 'invalid-worker-env');
      }
    }
    ensureManagedDirectory(this.dataDirectory, target);
    this.requireSpace((recipe.estimated_bytes || 0) + (this.runtimeRecipe.disk_space.safety_margin_bytes || 0));
    const env = { ...this.makeEnvironment(this.paths), UV_PROJECT_ENVIRONMENT: target };
    this.emit(context, safeProgress('installing', `Installing the trusted ${selection.workerProfile} worker environment.`));
    await this.runProcess(this.paths.uvExecutable, ['sync', '--locked', '--no-dev', '--no-install-project', '--python', context.managedPython || await this.findPython(context), '--project', project], context, { cwd: project, env });
    const result = await this.runProcess(python, ['-I', '-c', probe], context, { cwd: project });
    parseProbe(result, 'SPEECHCRAFT_WORKER_ENV_PROBE', 'WORKER_ENVIRONMENT_INVALID');
    this.setComponent(context, 'worker_environment', { state: 'verified', identity: `${this.runtimeRecipe.worker.distribution}/v${recipe.environment_version}/${selection.workerProfile}`,
      verification: 'trusted_lock_and_import_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null, path: target });
  }

  async installApprovedModels(context) {
    const profile = selectRuntimeProfile(this.environment);
    const root = runtimeRoots(this.dataDirectory).models;
    validateManagedStorage(this.environment, [root, this.paths.hfHome, this.paths.hfHub, this.paths.hfXet, this.paths.temporary]);
    const python = this.getManagerPython();
    for (const [role, component] of [['speech_detection', 'silero'], ['asr', 'whisper']]) {
      const modelName = role === 'asr' ? 'Whisper Large-v3' : 'Silero VAD';
      const tracker = new DownloadProgressTracker();
      this.setComponent(context, component, { state: 'downloading', failure_code: null, message: null,
        progress: null, progress_indeterminate: true,
        identity: role === 'asr' ? `Whisper Large-v3/${profile.modelPlatform}` : 'manifest-pinned Silero ONNX' });
      let result;
      let progressBuffer = '';
      let stallTimer;
      const publish = (telemetry, { force = false } = {}) => {
        if (!telemetry || !tracker.shouldEmit(telemetry, { force })) return;
        const sampled = { ...telemetry, sampled_at: new Date().toISOString() };
        const phase = sampled.phase;
        const componentState = phase === 'verifying' ? 'verifying_files'
          : phase === 'installing' ? 'installing' : 'downloading';
        const percent = phase === 'downloading' && Number.isSafeInteger(sampled.downloaded_bytes)
          && Number.isSafeInteger(sampled.total_bytes) && sampled.total_bytes > 0
          ? Math.max(0, Math.min(100, Math.floor(sampled.downloaded_bytes * 100 / sampled.total_bytes))) : null;
        const message = sampled.stalled ? `${modelName} download stalled; waiting for network data.`
          : phase === 'downloading' ? `Downloading ${modelName}.`
            : phase === 'verifying' ? `Verifying ${modelName} files.` : `Installing ${modelName}.`;
        this.setComponent(context, component, { state: componentState, failure_code: null, message,
          progress: sampled, progress_indeterminate: sampled.transfer_indeterminate });
        this.emit(context, { ...sampled, current: sampled.downloaded_bytes, total: sampled.total_bytes,
          percent, indeterminate: sampled.transfer_indeterminate, message });
      };
      stallTimer = setInterval(() => publish(tracker.stalled(), { force: true }), 1_000);
      try {
        result = await this.runProcess(python, ['-I', '-m', 'speechcraft_models', 'install', role,
          '--platform', profile.modelPlatform, '--root', root], context, { cwd: this.dataDirectory,
          env: { ...this.makeEnvironment(this.paths), SPEECHCRAFT_MODELS_ROOT: root },
          onOutput: (stream, chunk) => {
            if (stream !== 'stderr') return;
            progressBuffer = `${progressBuffer}${chunk.toString('utf8')}`.slice(-16_384);
            const lines = progressBuffer.split(/\r?\n/);
            progressBuffer = lines.pop() || '';
            for (const line of lines) {
              try {
                const event = JSON.parse(line);
                const telemetry = tracker.accept(event, role, modelName);
                if (!telemetry) continue;
                publish(telemetry, { force: ['downloading', 'source_acquiring', 'file_verified', 'source_file_acquired',
                  'retrying', 'verifying', 'publishing', 'installed'].includes(event.event) });
              } catch {}
            }
          } });
      } catch (error) {
        this.setComponent(context, component, { state: 'failed', failure_code: error.code || 'MODEL_INSTALL_FAILED',
          message: error.message.slice(0, 320), runtime_ready: false,
          progress: null, progress_indeterminate: false });
        throw error;
      } finally {
        clearInterval(stallTimer);
        tracker.reset();
      }
      let report;
      const latestProgress = context.componentStatus.components?.[component]?.progress;
      if (latestProgress) this.setComponent(context, component, { state: 'verifying_files', message: `Checking ${modelName} verification results.`,
        progress: { ...latestProgress, phase: 'verifying', bytes_per_second: null, eta_seconds: null,
          transfer_indeterminate: true, stalled: false, sampled_at: new Date().toISOString() }, progress_indeterminate: true });
      try { report = JSON.parse(result.stdout); } catch {
        this.setComponent(context, component, { state: 'failed', failure_code: 'MODEL_MANAGER_RESPONSE_INVALID',
          message: `The model manager returned an invalid verification report for ${role}.`, runtime_ready: false,
          progress: null, progress_indeterminate: false });
        throw new BootstrapError('MODEL_MANAGER_RESPONSE_INVALID', `The model manager returned an invalid verification report for ${role}.`);
      }
      if (report.install_ready !== true || report.status !== 'verified') {
        this.setComponent(context, component, { state: 'failed', failure_code: 'MODEL_FILES_NOT_VERIFIED',
          message: `Model files for ${role} did not pass the trusted manifest checks.`, runtime_ready: false,
          progress: null, progress_indeterminate: false });
        throw new BootstrapError('MODEL_FILES_NOT_VERIFIED', `Model files for ${role} did not pass the trusted manifest checks.`);
      }
      this.setComponent(context, component, { state: 'files_verified', verification: 'manifest_size_and_sha256', last_verified_at: new Date().toISOString(), failure_code: null, message: 'Files are verified; runtime inference has not been tested.', runtime_ready: false, progress: null, progress_indeterminate: false, path: report.root, report });
    }

    // Community-1 is the only credential-gated role. Check the local trusted
    // catalog report first, so verified snapshots never require another token.
    const managerPython = this.getManagerPython();
    const modelStatus = await this.runProcess(managerPython, ['-I', '-m', 'speechcraft_models', 'status', 'diarization',
      '--platform', profile.modelPlatform, '--root', root], context, { cwd: this.dataDirectory,
      env: { ...this.makeEnvironment(this.paths), SPEECHCRAFT_MODELS_ROOT: root }, allowFailure: true });
    let localReport;
    try { localReport = JSON.parse(modelStatus.stdout); }
    catch { throw new BootstrapError('MODEL_MANAGER_RESPONSE_INVALID', 'The model manager returned an invalid Community-1 status report.'); }
    if (modelStatus.code === 0 && localReport.install_ready === true && localReport.integrity_verified === true) {
      this.setComponent(context, 'pyannote', { state: 'files_verified', identity: 'pyannote/speaker-diarization-community-1@3533c8cf8e369892e6b79ff1bf80f7b0286a54ee',
        verification: 'complete_manifest_size_sha256_and_exact_tree', last_verified_at: new Date().toISOString(),
        path: localReport.root, report: localReport, runtime_ready: false, failure_code: null,
        message: 'Community-1 files are verified; offline inference validation is pending.' });
      return;
    }
    if (!context.authorizationToken) {
      const previous = context.bootstrapStatus?.components?.pyannote;
      const afterRestart = previous?.failure_code === 'AUTHORIZATION_REQUIRED_AFTER_RESTART'
        || ['downloading', 'checking_token', 'checking_model_access', 'verifying_files', 'interrupted'].includes(previous?.state)
        || context.bootstrapStatus?.recovery?.authorization_interrupted === true;
      const status = { ...context.bootstrapStatus, components: { ...(context.bootstrapStatus?.components || {}) } };
      status.components.pyannote = { state: 'waiting_for_token', identity: 'pyannote/speaker-diarization-community-1@3533c8cf8e369892e6b79ff1bf80f7b0286a54ee',
        verification: 'pending', runtime_ready: false, failure_code: afterRestart ? 'AUTHORIZATION_REQUIRED_AFTER_RESTART' : null,
        message: afterRestart ? 'The controller restarted during gated acquisition. Its token was discarded; submit a read token to resume.'
          : 'Accept Community-1 conditions and submit a Hugging Face read token to install this model.' };
      this.setComponent(context, 'pyannote', status.components.pyannote);
      context.onComponentStatus?.(status);
      context.waitForUser?.(status, 'Community-1 needs Hugging Face authorization.');
      return;
    }

    this.setComponent(context, 'pyannote', { state: 'checking_token', identity: 'pyannote/speaker-diarization-community-1@3533c8cf8e369892e6b79ff1bf80f7b0286a54ee',
      verification: 'pending', runtime_ready: false, failure_code: null, message: 'Checking token identity and Community-1 access.' });
    let progressBuffer = '';
    const tracker = new DownloadProgressTracker();
    const publishGated = (telemetry, force = false) => {
      if (!telemetry || !tracker.shouldEmit(telemetry, { force })) return;
      const sampled = { ...telemetry, sampled_at: new Date().toISOString() };
      const phase = sampled.phase;
      const percent = phase === 'downloading' && Number.isSafeInteger(sampled.downloaded_bytes)
        && Number.isSafeInteger(sampled.total_bytes) && sampled.total_bytes > 0
        ? Math.max(0, Math.min(100, Math.floor(sampled.downloaded_bytes * 100 / sampled.total_bytes))) : null;
      const message = phase === 'downloading' ? 'Downloading the pinned Community-1 snapshot.'
        : phase === 'verifying' ? 'Verifying Community-1 files.' : 'Installing Community-1.';
      this.setComponent(context, 'pyannote', { state: phase === 'verifying' ? 'verifying_files'
        : phase === 'installing' ? 'installing' : 'downloading', failure_code: null,
      message, progress: sampled, progress_indeterminate: sampled.transfer_indeterminate });
      this.emit(context, { ...sampled, current: sampled.downloaded_bytes, total: sampled.total_bytes,
        percent, indeterminate: sampled.transfer_indeterminate, message });
    };
    const credentialBytes = Buffer.isBuffer(context.authorizationToken)
      ? context.authorizationToken : Buffer.from(String(context.authorizationToken), 'utf8');
    const tokenInput = Buffer.concat([credentialBytes, Buffer.from('\n')]);
    let acquired;
    try { acquired = await this.runProcess(managerPython, ['-I', '-m', 'speechcraft_models', 'authorize-install', 'diarization',
      '--platform', profile.modelPlatform, '--root', root, '--token-stdin'], context, {
      cwd: this.dataDirectory,
      env: { ...this.makeEnvironment(this.paths), SPEECHCRAFT_MODELS_ROOT: root },
      stdinData: tokenInput, allowFailure: true,
      onOutput: (stream, chunk) => {
        if (stream !== 'stderr') return;
        progressBuffer = `${progressBuffer}${chunk.toString('utf8')}`.slice(-16_384);
        const lines = progressBuffer.split(/\r?\n/); progressBuffer = lines.pop() || '';
        for (const line of lines) {
          try {
            const event = JSON.parse(line);
            if (event.event === 'authorization_status' && ['checking_token', 'checking_model_access', 'downloading'].includes(event.state)) {
              this.setComponent(context, 'pyannote', { state: event.state, failure_code: null,
                message: event.state === 'checking_token' ? 'Checking Hugging Face token identity.'
                  : event.state === 'checking_model_access' ? 'Checking access to the pinned Community-1 repository.'
                    : 'Downloading the pinned Community-1 snapshot.',
                progress: event.state === 'downloading' ? { phase: 'downloading', role: 'diarization',
                  model_name: 'pyannote Community-1', current_filename: null, downloaded_bytes: null,
                  total_bytes: null, bytes_per_second: null, eta_seconds: null,
                  files_completed: null, files_total: null, transfer_indeterminate: true, stalled: false,
                  sampled_at: new Date().toISOString() } : null, progress_indeterminate: event.state === 'downloading' });
              continue;
            }
            const telemetry = tracker.accept(event, 'diarization', 'pyannote Community-1');
            if (!telemetry) continue;
            publishGated(telemetry, ['downloading', 'source_acquiring', 'source_file_acquired', 'retrying',
              'verifying', 'publishing', 'installed'].includes(event.event));
          } catch {}
        }
      },
    }); } finally {
      tokenInput.fill(0);
      if (credentialBytes !== context.authorizationToken) credentialBytes.fill(0);
      tracker.reset();
    }
    let gatedReport;
    const gatedProgress = context.componentStatus.components?.pyannote?.progress;
    if (gatedProgress) this.setComponent(context, 'pyannote', { state: 'verifying_files',
      message: 'Checking Community-1 verification results.', progress: { ...gatedProgress, phase: 'verifying',
        bytes_per_second: null, eta_seconds: null, transfer_indeterminate: true, stalled: false,
        sampled_at: new Date().toISOString() }, progress_indeterminate: true });
    try { gatedReport = JSON.parse(acquired.stdout); }
    catch {
      this.setComponent(context, 'pyannote', { state: 'failed', failure_code: 'MODEL_MANAGER_RESPONSE_INVALID',
        message: 'The model manager returned an invalid Community-1 authorization result.', runtime_ready: false,
        progress: null, progress_indeterminate: false });
      throw new BootstrapError('MODEL_MANAGER_RESPONSE_INVALID', 'The model manager returned an invalid Community-1 authorization result.');
    }
    if (acquired.code !== 0 || gatedReport.status === 'error') {
      const allowedCodes = new Set(['HF_TOKEN_INVALID', 'HF_MODEL_ACCESS_REQUIRED', 'HF_TOKEN_PERMISSION_DENIED',
        'HF_APPROVAL_PENDING', 'HF_NETWORK_ERROR', 'HF_RATE_LIMITED', 'HF_SNAPSHOT_INCOMPLETE',
        'HF_SNAPSHOT_INTEGRITY_FAILED', 'MODEL_STORAGE_INSUFFICIENT']);
      const code = allowedCodes.has(gatedReport.error_code) ? gatedReport.error_code : 'MODEL_INSTALL_FAILED';
      const safeMessages = {
        HF_TOKEN_INVALID: 'Hugging Face rejected this token. Check it and submit a valid read token.',
        HF_MODEL_ACCESS_REQUIRED: 'Accept the Community-1 conditions and confirm account access on Hugging Face, then retry.',
        HF_TOKEN_PERMISSION_DENIED: 'This token lacks read permission. Create a read token and retry.',
        HF_APPROVAL_PENDING: 'Community-1 access is pending on Hugging Face. Complete its approval steps and retry.',
        HF_NETWORK_ERROR: 'Hugging Face could not complete the request. Check the connection and retry.',
        HF_RATE_LIMITED: 'Hugging Face is rate limiting this request. Wait briefly and retry.',
        HF_SNAPSHOT_INCOMPLETE: 'The pinned Community-1 snapshot is missing a required file. Retry later or contact support.',
        HF_SNAPSHOT_INTEGRITY_FAILED: 'A downloaded Community-1 file failed integrity verification. Retry the installation.',
        MODEL_STORAGE_INSUFFICIENT: 'There is not enough free storage for Community-1. Free disk space and retry.',
        MODEL_INSTALL_FAILED: 'Community-1 could not be installed. Review the sanitized diagnostics and retry.',
      };
      const waiting = ['HF_TOKEN_INVALID', 'HF_MODEL_ACCESS_REQUIRED', 'HF_TOKEN_PERMISSION_DENIED', 'HF_APPROVAL_PENDING'].includes(code);
      this.setComponent(context, 'pyannote', { state: waiting ? 'access_denied' : 'failed', failure_code: code,
        message: safeMessages[code], runtime_ready: false, progress: null, progress_indeterminate: false });
      const error = new BootstrapError(code, safeMessages[code]);
      throw error;
    }
    if (gatedReport.install_ready !== true || gatedReport.integrity_verified !== true || gatedReport.inventory_exact !== true) {
      this.setComponent(context, 'pyannote', { state: 'failed', failure_code: 'HF_SNAPSHOT_INTEGRITY_FAILED',
        message: 'The complete Community-1 snapshot did not pass its trusted exact-tree checks.', runtime_ready: false,
        progress: null, progress_indeterminate: false });
      throw new BootstrapError('HF_SNAPSHOT_INTEGRITY_FAILED', 'The complete Community-1 snapshot did not pass trusted exact-tree verification.');
    }
    this.setComponent(context, 'pyannote', { state: 'files_verified', identity: 'pyannote/speaker-diarization-community-1@3533c8cf8e369892e6b79ff1bf80f7b0286a54ee',
      verification: 'complete_manifest_size_sha256_and_exact_tree', last_verified_at: new Date().toISOString(),
      path: gatedReport.root, report: gatedReport, runtime_ready: false, failure_code: null,
      message: 'Community-1 files are verified; offline inference validation is pending.', progress: null,
      progress_indeterminate: false });
  }

  async validateRuntime(context) {
    const probes = this.runtimeRecipe.runtime_validation?.probes;
    if (!this.runtimeRecipe.runtime_validation?.available || !Array.isArray(probes) || probes.length === 0) {
      const reason = this.runtimeRecipe.runtime_validation?.reason || 'No trusted inference validation helper is configured.';
      this.setComponent(context, 'runtime_probes', { state: 'blocked', failure_code: 'runtime_validation_pending', message: reason });
      this.status(context, { runtime_validation_pending: true, production_ready: false });
      return;
    }
    const validated = [];
    const blocked = [];
    const components = context.componentStatus.components || {};
    const managedRoot = path.resolve(this.dataDirectory);
    const managedPath = (candidate) => {
      if (typeof candidate !== 'string' || !candidate) return '';
      const resolved = path.resolve(candidate);
      const relative = path.relative(managedRoot, resolved);
      return relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) ? '' : resolved;
    };
    let probeAudio = '';
    if (this.runtimeProbeAudio) {
      try {
        const candidate = path.resolve(this.runtimeProbeAudio);
        const stat = fs.lstatSync(candidate);
        if (stat.isFile() && !stat.isSymbolicLink() && stat.size <= 100 * 1024 * 1024) probeAudio = candidate;
      } catch {}
    }
    const modelPlatform = selectRuntimeProfile(this.environment).modelPlatform;
    const modelPaths = {};
    for (const [role, name] of [['asr', 'whisper'], ['speech_detection', 'silero'], ['speaker_purity', 'titanet'], ['diarization', 'pyannote']]) {
      const item = components[name];
      const candidate = item?.report?.root || item?.path;
      if (candidate && managedPath(candidate)) modelPaths[role] = managedPath(candidate);
    }
    const helperEnvironment = { ...this.makeEnvironment(this.paths),
      HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1', HF_HUB_DISABLE_IMPLICIT_TOKEN: '1',
      SPEECHCRAFT_MANAGED_RUNTIME: '1', SPEECHCRAFT_MODEL_ROOT: path.join(this.dataDirectory, 'models'),
      SPEECHCRAFT_MODEL_PLATFORM: modelPlatform,
      SPEECHCRAFT_ASR_MODEL_PATH: modelPaths.asr || '',
      SPEECHCRAFT_SILERO_ONNX_PATH: modelPaths.speech_detection ? path.join(modelPaths.speech_detection, 'silero_vad.onnx') : '',
      SPEECHCRAFT_TITANET_ONNX_PATH: modelPaths.speaker_purity ? path.join(modelPaths.speaker_purity, 'nemo_en_titanet_large.onnx') : '',
      SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH: modelPaths.diarization || '',
      SPEECHCRAFT_FFMPEG_BIN: ['verified', 'files_verified', 'runtime_ready'].includes(components.ffmpeg?.state) && managedPath(components.ffmpeg.path) ? path.join(components.ffmpeg.path, 'ffmpeg') : '',
      SPEECHCRAFT_FFPROBE_BIN: ['verified', 'files_verified', 'runtime_ready'].includes(components.ffmpeg?.state) && managedPath(components.ffmpeg.path) ? path.join(components.ffmpeg.path, 'ffprobe') : '',
    };
    const optionalBlocked = [];
    let cudaReport = null;
    let cudaSelected = false;
    for (const probe of probes) {
      if (!/^[a-z][a-z0-9_]*$/.test(probe.component || '') || !/^[a-f0-9]{64}$/.test(probe.sha256 || '')
          || !Number.isSafeInteger(probe.timeout_ms) || probe.timeout_ms < 1000 || probe.timeout_ms > 10 * 60_000
          || !Array.isArray(probe.arguments) || probe.arguments.some((arg) => typeof arg !== 'string' || arg.length > 200)) {
        throw new BootstrapError('RUNTIME_PROBE_RECIPE_INVALID', 'A trusted runtime-validation recipe is malformed.');
      }
      let selectedProbeAudio = probeAudio;
      if (!selectedProbeAudio) {
        const fixture = this.runtimeRecipe.runtime_validation.fixtures?.[probe.fixture];
        const fixturePath = fixture && path.resolve(this.applicationRoot, fixture.path);
        const fixtureRelative = fixturePath && path.relative(this.applicationRoot, fixturePath);
        const safeFixture = fixture && fixturePath && fixtureRelative !== '..'
          && !fixtureRelative.startsWith(`..${path.sep}`) && !path.isAbsolute(fixtureRelative)
          && Number.isSafeInteger(fixture.size_bytes) && fixture.size_bytes > 0
          && /^[a-f0-9]{64}$/.test(fixture.sha256 || '') && hashMatches(fixturePath, fixture.sha256);
        if (!safeFixture) {
          this.setComponent(context, probe.component, { state: 'blocked', runtime_ready: false,
            failure_code: 'runtime_probe_fixture_invalid', message: 'A bundled runtime probe fixture is missing or failed its trusted integrity check.' });
          (probe.component === 'cuda' ? optionalBlocked : blocked).push(probe.component);
          continue;
        }
        selectedProbeAudio = fixturePath;
      }
      const helper = path.resolve(this.applicationRoot, probe.helper);
      const relative = path.relative(this.applicationRoot, helper);
      if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) || !trustedFileMatches(helper, probe.sha256)) {
        throw new BootstrapError('RUNTIME_PROBE_INTEGRITY_FAILED', 'A runtime-validation helper is outside the application or failed its trusted digest check.');
      }
      const modelComponent = ['silero', 'whisper', 'pyannote', 'titanet'].includes(probe.component);
      this.setComponent(context, probe.component, { state: modelComponent ? 'validating_inference' : 'validating',
        failure_code: null, message: null,
        ...(modelComponent ? { progress: { phase: 'running_inference', role: ({ silero: 'speech_detection', whisper: 'asr', pyannote: 'diarization', titanet: 'speaker_purity' })[probe.component],
          model_name: ({ silero: 'Silero VAD', whisper: 'Whisper Large-v3', pyannote: 'pyannote Community-1', titanet: 'TitaNet-Large' })[probe.component],
          current_filename: null, downloaded_bytes: null, total_bytes: null, bytes_per_second: null,
          eta_seconds: null, files_completed: null, files_total: null, transfer_indeterminate: true, stalled: false,
          sampled_at: new Date().toISOString() }, progress_indeterminate: true } : {}) });
      const role = probe.environment_role || 'model_manager';
      if (!['model_manager', 'backend', 'worker', 'worker_cuda_candidate'].includes(role)) throw new BootstrapError('RUNTIME_PROBE_RECIPE_INVALID', 'A runtime probe references an unapproved interpreter role.');
      const components = context.componentStatus.components || {};
      const environmentRoot = path.resolve(this.dataDirectory, 'environments');
      const python = role === 'model_manager' ? this.getManagerPython()
        : role === 'backend' ? (components.backend_environment?.state === 'verified' && components.backend_environment.path
          ? path.join(components.backend_environment.path, 'bin', 'python') : null)
        : role === 'worker_cuda_candidate' ? (components.worker_cuda_candidate?.state === 'verified' && components.worker_cuda_candidate.path
          ? path.join(components.worker_cuda_candidate.path, 'bin', 'python') : null)
        : (components.worker_environment?.state === 'verified' && components.worker_environment.path
            ? path.join(components.worker_environment.path, 'bin', 'python') : null);
      if (!python || !path.resolve(python).startsWith(`${environmentRoot}${path.sep}`)) {
        if (probe.component === 'cuda') {
          this.setComponent(context, 'cuda', { state: 'blocked', runtime_ready: false, failure_code: 'cuda_candidate_unavailable',
            message: 'No verified managed CUDA worker candidate is available; the validated CPU profile remains selected.' });
          optionalBlocked.push('cuda');
          continue;
        }
        throw new BootstrapError('RUNTIME_PROBE_ENVIRONMENT_UNAVAILABLE', `The managed ${role} environment is not verified for this probe.`);
      }
      const timer = AbortSignal.timeout(probe.timeout_ms);
      const signal = context.signal ? AbortSignal.any([context.signal, timer]) : timer;
      let result;
      const candidateGpus = context.componentStatus.host_details?.nvidia_gpus || [];
      const probeLibraryPaths = role === 'worker_cuda_candidate'
        ? components.worker_cuda_candidate?.library_paths
        : (cudaSelected ? components.worker_environment?.library_paths : null);
      const pyannoteCudaValidated = context.componentStatus.runtime_profile?.pyannote_cuda_validated === true;
      const probeEnvironment = { ...helperEnvironment, SPEECHCRAFT_RUNTIME_PROBE_AUDIO: selectedProbeAudio,
        SPEECHCRAFT_PROBE_DEVICE: probe.component === 'cuda' || (probe.component === 'whisper' && cudaSelected)
          || (probe.component === 'pyannote' && pyannoteCudaValidated) ? 'cuda' : 'cpu',
        SPEECHCRAFT_PYANNOTE_DEVICE: pyannoteCudaValidated ? 'cuda' : 'cpu',
        SPEECHCRAFT_PYANNOTE_CUDA_VALIDATED: pyannoteCudaValidated ? '1' : '0',
        SPEECHCRAFT_ASR_DEVICE: cudaSelected ? 'cuda' : 'cpu',
        LD_LIBRARY_PATH: Array.isArray(probeLibraryPaths) ? probeLibraryPaths.join(path.delimiter) : '',
        SPEECHCRAFT_CUDA_DRIVER: Array.isArray(candidateGpus) ? String(candidateGpus[0] || '').slice(0, 160) : '' };
      try { result = await this.runProcess(python, ['-I', helper, ...probe.arguments], { ...context, signal },
        { cwd: this.applicationRoot, env: probeEnvironment, allowFailure: true }); }
      catch (error) {
        if (timer.aborted && !context.signal?.aborted) throw new BootstrapError('RUNTIME_PROBE_TIMEOUT', `The ${probe.component} runtime probe exceeded its trusted time limit.`, { cause: error });
        throw error;
      }
      let report;
      try { report = JSON.parse(result.stdout); } catch {
        report = { success: false, error_code: 'RUNTIME_PROBE_RESPONSE_INVALID', error: 'Probe returned invalid JSON.' };
      }
      if (probe.component === 'cuda') cudaReport = report;
      if (result.code !== 0 || report.schema_version !== 1 || report.component !== probe.component || report.success !== true
          || report.inference_completed !== true || typeof report.identity !== 'string' || !report.identity) {
        const code = typeof report.error_code === 'string' && /^[a-z][a-z0-9_]{1,79}$/.test(report.error_code)
          ? report.error_code : 'runtime_probe_failed';
        this.setComponent(context, probe.component, { state: 'blocked', runtime_ready: false,
          failure_code: code, message: typeof report.error === 'string' ? report.error.slice(0, 320)
            : `The ${probe.component} probe did not confirm successful inference.`,
          progress: null, progress_indeterminate: false,
          ...(probe.component === 'cuda' ? { report: {
            schema_version: report.schema_version || null, gpu_detected: report.gpu_detected === true,
            driver_identification: typeof report.driver_identification === 'string' ? report.driver_identification.slice(0, 160) : null,
            torch_cuda_success: report.torch_cuda_success === true,
            ctranslate2_cuda_success: report.ctranslate2_cuda_success === true,
            required_libraries: report.required_libraries && typeof report.required_libraries === 'object' ? report.required_libraries : {},
            actual_execution_device: typeof report.actual_execution_device === 'string' ? report.actual_execution_device.slice(0, 160) : null,
            failure_code: code, fallback_decision: 'retain-validated-cpu',
            diagnostic: typeof report.diagnostic === 'string' ? report.diagnostic.slice(0, 240) : null,
          } } : {}) });
        if (probe.component === 'cuda') optionalBlocked.push('cuda');
        else blocked.push(probe.component);
        continue;
      }
      if (probe.component === 'cuda') {
        const selection = selectRuntimeProfile(this.environment, { gpuDetected: true, cudaValidated: true, cudaProbe: report });
        cudaSelected = selection.cuda_validated === true;
        if (cudaSelected) {
          const candidate = context.componentStatus.components.worker_cuda_candidate;
          this.setComponent(context, 'worker_environment', { ...candidate, state: 'verified',
            identity: `${this.runtimeRecipe.worker.distribution}/v${this.runtimeRecipe.worker.profiles['linux-x86_64-cuda'].environment_version}/linux-x86_64-cuda`,
            effective_profile: 'linux-x86_64-cuda', device: report.device });
          // The ordinary Whisper probe runs before CUDA is considered, against
          // the safe CPU environment. Once the dedicated CUDA probe proves a
          // real Large-v3 transcription, expose that as the selected runtime
          // device rather than leaving status to imply Whisper will run on CPU.
          const whisper = context.componentStatus.components.whisper;
          if (whisper?.state === 'runtime_ready') {
            this.setComponent(context, 'whisper', { ...whisper, device: report.device,
              effective_profile: 'linux-x86_64-cuda',
              verification: 'trusted_cpu_and_cuda_inference_probes',
              cuda_inference: { backend: report.backend, device: report.actual_execution_device,
                measurements: report.measurements } });
          }
          this.status(context, { runtime_profile: { ...selection, cuda_probe: report }, accelerator: 'cuda' });
        }
      }
      this.setComponent(context, probe.component, { state: 'runtime_ready', identity: report.identity,
        verification: 'trusted_inference_probe', last_verified_at: new Date().toISOString(), failure_code: null, message: null,
        runtime_ready: true, progress: null, progress_indeterminate: false,
        device: typeof report.device === 'string' ? report.device : null });
      validated.push(probe.component);
    }
    if (!cudaSelected && this.environment?.profile === 'linux-x86_64') {
      this.status(context, { runtime_profile: selectRuntimeProfile(this.environment, {
        gpuDetected: context.componentStatus.host_details?.cuda_candidate === true, cudaValidated: false,
      }), accelerator: 'cpu', cuda_fallback_reason: cudaReport?.error || 'CUDA inference did not pass the trusted runtime probe.' });
    }
    this.setComponent(context, 'runtime_probes', { state: blocked.length ? 'blocked' : 'verified',
      verification: 'trusted_inference_probes', components: validated, blocked_components: blocked,
      last_verified_at: new Date().toISOString(), failure_code: blocked.length ? 'runtime_probe_blocked' : null,
      optional_blocked_components: optionalBlocked,
      message: blocked.length ? `Runtime probes remain blocked for: ${blocked.join(', ')}.` : null });
    this.status(context, { runtime_validation_pending: blocked.length > 0, production_ready: false });
  }

  markServicesBlocked(context) {
    this.setComponent(context, 'backend_service', { state: 'blocked', failure_code: 'service_supervision_unavailable', message: 'The authenticated controller does not yet own a verified backend service process. It will not infer service readiness from a process or import probe.' });
    this.status(context, { production_ready: false });
  }

  verifyServices(context) {
    this.setComponent(context, 'backend_service', { state: 'blocked', failure_code: 'service_health_unavailable', message: 'No supervised backend health check completed.' });
    this.status(context, { bootstrap_ready: Boolean(context.componentStatus.uv_verified && context.componentStatus.python_verified && context.componentStatus.model_manager_verified), production_ready: false });
  }
}
