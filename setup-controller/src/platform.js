import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';

// glibc 2.28 is the minimum declared by the pinned FFmpeg linux-x86_64 build
// (setup-controller/resources/runtime-manifest.json). The pinned uv, CPython
// and Node runtimes are linux-gnu builds for that same baseline. Ubuntu 22.04
// and 24.04 are the guests that meet it and are accepted for WSL2.
export const MINIMUM_GLIBC = '2.28';
export const SUPPORTED_UBUNTU_VERSIONS = Object.freeze(['22.04', '24.04']);
export const WSL_LAUNCH_FREE_BYTES = 512 * 1024 * 1024;

const WSL_REDIRECT_KEYS = Object.freeze([
  'HF_HOME', 'HUGGINGFACE_HUB_CACHE', 'HF_HUB_CACHE', 'HF_XET_CACHE', 'TRANSFORMERS_CACHE',
  'TMPDIR', 'TEMP', 'TMP', 'UV_CACHE_DIR', 'UV_PYTHON_INSTALL_DIR', 'XDG_CACHE_HOME',
]);

export class PlatformError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'PlatformError';
    this.code = code;
  }
}

export function decodeMountField(value) {
  return value.replace(/\\([0-7]{3})/g, (_match, octal) => String.fromCharCode(Number.parseInt(octal, 8)));
}

export function parseMountInfo(contents) {
  return contents.split('\n').filter(Boolean).map((line) => {
    const separator = line.indexOf(' - ');
    if (separator < 0) throw new PlatformError('MOUNT_INFO_INVALID', 'WSL mount information is malformed.');
    const left = line.slice(0, separator).split(' ');
    const right = line.slice(separator + 3).split(' ');
    if (left.length < 6 || right.length < 3) throw new PlatformError('MOUNT_INFO_INVALID', 'WSL mount information is incomplete.');
    return {
      root: decodeMountField(left[3]),
      mountPoint: path.resolve(decodeMountField(left[4])),
      options: left[5].split(','),
      fileSystem: right[0],
      source: decodeMountField(right[1]),
      superOptions: right[2].split(','),
    };
  });
}

export function isWslKernel({ platform = process.platform, release = '', version = '' } = {}) {
  // Kernel text is only a WSL-versus-native signal. WSL_INTEROP is intentionally
  // ignored: it is not proof of WSL2 or of a supported Ubuntu guest.
  return platform === 'linux' && /microsoft|wsl/i.test(`${release} ${version}`);
}

export function hostEnvironment({ platform = process.platform, arch = process.arch,
  kernelRelease, kernelVersion, mountInfoText } = {}) {
  if (platform === 'darwin' && arch === 'arm64') {
    return { os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false };
  }
  if (platform === 'linux' && arch === 'x64') {
    let release = kernelRelease;
    let version = kernelVersion;
    if (release === undefined) {
      try { release = fs.readFileSync('/proc/sys/kernel/osrelease', 'utf8'); }
      catch { release = ''; }
    }
    if (version === undefined) {
      try { version = fs.readFileSync('/proc/version', 'utf8'); }
      catch { version = ''; }
    }
    const windowsKernel = isWslKernel({ platform, release, version });
    const wsl = /microsoft-standard-WSL2/i.test(`${release} ${version}`);
    if (windowsKernel && !wsl) {
      throw new PlatformError('WSL_VERSION_UNSUPPORTED', 'SpeechCraft requires Ubuntu on WSL2. WSL1 is not supported; update the distribution to WSL2 before continuing.');
    }
    if (!wsl && !process.report?.getReport?.().header?.glibcVersionRuntime) {
      throw new PlatformError('PLATFORM_UNSUPPORTED', 'SpeechCraft bootstrap currently requires x86-64 glibc Linux or Apple Silicon macOS.');
    }
    return { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl };
  }
  if (platform === 'linux' && arch !== 'x64') {
    throw new PlatformError('PLATFORM_UNSUPPORTED', `SpeechCraft bootstrap does not support Linux architecture ${arch}.`);
  }
  throw new PlatformError('PLATFORM_UNSUPPORTED', `SpeechCraft bootstrap does not support ${platform}/${arch}.`);
}

function pathContains(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

function resolveManagedDirectory(directory) {
  const absolute = path.resolve(directory);
  try {
    const existing = fs.lstatSync(absolute);
    if (existing.isSymbolicLink()) {
      throw new PlatformError('STORAGE_PATH_UNSAFE', 'Managed bootstrap storage must be a real directory, not a symlink.');
    }
  } catch (error) {
    if (error instanceof PlatformError || error.code !== 'ENOENT') throw error;
  }
  fs.mkdirSync(absolute, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(absolute);
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new PlatformError('STORAGE_PATH_UNSAFE', 'Managed bootstrap storage must be a real directory, not a symlink.');
  }
  return fs.realpathSync(absolute);
}

function mountForPath(mounts, candidate) {
  return mounts.filter((mount) => pathContains(mount.mountPoint, candidate))
    .sort((left, right) => right.mountPoint.length - left.mountPoint.length)[0] || null;
}

function resolveTargetWithoutCreating(directory) {
  let candidate = path.resolve(directory);
  const suffix = [];
  while (!fs.existsSync(candidate)) {
    const parent = path.dirname(candidate);
    if (parent === candidate) throw new PlatformError('STORAGE_PATH_UNSAFE', 'Could not resolve a managed storage parent directory.');
    suffix.unshift(path.basename(candidate));
    candidate = parent;
  }
  return path.resolve(fs.realpathSync(candidate), ...suffix);
}

export function isWindowsBackedMount(mount) {
  const identity = `${mount.fileSystem} ${mount.source} ${mount.superOptions.join(' ')}`;
  return /^(9p|drvfs|ntfs|ntfs3|fuseblk|cifs|smbfs)$/i.test(mount.fileSystem)
    || /drvfs|aname=drvfs|path=[A-Z]:\\/i.test(identity);
}

function assertWslStorage(environment, resolved, { mountInfoText } = {}) {
  if (!environment.wsl) return resolved;
  let contents = mountInfoText;
  if (contents === undefined) {
    try { contents = fs.readFileSync('/proc/self/mountinfo', 'utf8'); }
    catch { throw new PlatformError('WSL_STORAGE_UNKNOWN', 'WSL was detected, but mount information is unavailable; refusing managed writes.'); }
  }
  const mounts = parseMountInfo(contents);
  for (const directory of resolved) {
    const mount = mountForPath(mounts, directory);
    if (!mount) throw new PlatformError('WSL_STORAGE_UNKNOWN', 'Could not identify the filesystem backing managed bootstrap storage; refusing writes.');
    if (isWindowsBackedMount(mount)) {
      throw new PlatformError('WSL_WINDOWS_MOUNT', 'SpeechCraft managed data, runtimes, environments, caches, and temporary files must be stored on the WSL Linux filesystem, not a Windows-mounted drive. Move the SpeechCraft data directory to WSL ext4.');
    }
  }
  return resolved;
}

export function managedStorageLocations(dataDirectory) {
  const root = path.resolve(dataDirectory);
  const application = path.join(root, 'application-data');
  const projects = path.join(application, 'projects');
  const media = path.join(projects, 'media');
  const cache = path.join(root, 'cache', 'bootstrap');
  return [
    root,
    path.join(root, 'models'),
    path.join(root, 'environments'),
    path.join(root, 'runtimes'),
    path.join(root, 'tools', 'ffmpeg'),
    path.join(root, 'staging'),
    path.join(root, 'logs'),
    cache,
    path.join(cache, 'huggingface'),
    path.join(cache, 'huggingface', 'hub'),
    path.join(cache, 'huggingface', 'xet'),
    path.join(cache, 'uv'),
    path.join(cache, 'tmp'),
    path.join(cache, 'home'),
    path.join(root, 'staging', 'bootstrap'),
    path.join(root, 'quarantine', 'bootstrap'),
    application,
    projects,
    media,
    path.join(media, 'sources'),
    path.join(media, 'clips'),
    path.join(projects, 'exports'),
  ];
}

export function parseOsRelease(contents) {
  const values = {};
  for (const line of String(contents).split('\n')) {
    const match = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (!match) continue;
    let value = match[2];
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) value = value.slice(1, -1);
    values[match[1]] = value;
  }
  return values;
}

export function compareDottedVersions(left, right) {
  const parse = (value) => String(value).split('.').map((part) => {
    if (!/^\d+$/.test(part)) throw new PlatformError('GLIBC_UNKNOWN', `Cannot compare version ${value}.`);
    return Number(part);
  });
  const a = parse(left);
  const b = parse(right);
  const length = Math.max(a.length, b.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (a[index] || 0) - (b[index] || 0);
    if (difference !== 0) return difference < 0 ? -1 : 1;
  }
  return 0;
}

export function assertCurrentWslGuest(environment, options = {}) {
  if (!environment?.wsl) return null;
  const architecture = options.architecture || environment.architecture;
  if (architecture !== 'x86_64' && architecture !== 'x64') {
    throw new PlatformError('ARCHITECTURE_UNSUPPORTED', `SpeechCraft inside WSL requires Linux x86-64. This guest is ${architecture}.`);
  }
  let text = options.osReleaseText;
  if (text === undefined) {
    try { text = fs.readFileSync('/etc/os-release', 'utf8'); }
    catch { throw new PlatformError('WSL_GUEST_UNKNOWN', 'Ubuntu release information is unavailable. SpeechCraft will not treat a kernel name or WSL_INTEROP as proof of a supported distribution.'); }
  }
  const parsed = parseOsRelease(text);
  if (parsed.ID !== 'ubuntu') {
    throw new PlatformError('DISTRO_UNSUPPORTED', `SpeechCraft on Windows requires Ubuntu 22.04 or 24.04 under WSL2. This guest reports ${parsed.ID || 'an unknown distribution'}.`);
  }
  if (!SUPPORTED_UBUNTU_VERSIONS.includes(parsed.VERSION_ID)) {
    throw new PlatformError('UBUNTU_RELEASE_UNSUPPORTED', `Ubuntu ${parsed.VERSION_ID || 'unknown'} is not a supported SpeechCraft guest. Use Ubuntu 22.04 or 24.04 on WSL2.`);
  }
  const glibc = options.glibcVersion || process.report?.getReport?.().header?.glibcVersionRuntime;
  if (!glibc) throw new PlatformError('GLIBC_UNKNOWN', 'The guest glibc version could not be read. SpeechCraft will not continue without that compatibility check.');
  if (compareDottedVersions(glibc, MINIMUM_GLIBC) < 0) {
    throw new PlatformError('GLIBC_INCOMPATIBLE', `This guest glibc ${glibc} is older than the required ${MINIMUM_GLIBC}.`);
  }
  if (options.freeBytes !== undefined && options.requiredBytes !== undefined && options.freeBytes < options.requiredBytes) {
    throw new PlatformError('STORAGE_CAPACITY_INSUFFICIENT', `The guest filesystem has ${options.freeBytes} bytes free; SpeechCraft needs at least ${options.requiredBytes} bytes before launch.`);
  }
  if (options.loopbackAvailable === false) {
    throw new PlatformError('LOOPBACK_UNAVAILABLE', 'IPv4 loopback is not available. SpeechCraft keeps its services on 127.0.0.1 and will not bind them to a public interface.');
  }
  if (options.processGroupAvailable === false) {
    throw new PlatformError('PROCESS_GROUP_UNAVAILABLE', 'This environment cannot signal process groups, so SpeechCraft cannot safely own and stop its services.');
  }
  return { id: 'ubuntu', version: parsed.VERSION_ID, glibc, architecture: 'x86_64' };
}

export function probeLoopback() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.listen(0, '127.0.0.1', () => server.close(() => resolve(true)));
  });
}

export function probeProcessGroup(spawnImpl = spawn) {
  const child = spawnImpl(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' });
  return new Promise((resolve) => {
    const finish = (ok) => {
      try { if (child.pid) process.kill(-child.pid, 'SIGKILL'); } catch { /* already gone */ }
      child.unref?.();
      resolve(ok);
    };
    child.once('error', () => finish(false));
    child.once('spawn', () => {
      try { process.kill(-child.pid, 0); finish(true); }
      catch { finish(false); }
    });
  });
}

export function freeBytes(directory) {
  const stat = fs.statfsSync(directory);
  return stat.bavail * stat.bsize;
}

export function stripWindowsPathEntries(pathValue) {
  const kept = [];
  const parts = String(pathValue).split(':');
  for (let index = 0; index < parts.length; index += 1) {
    const entry = parts[index];
    const next = parts[index + 1];
    // A Windows drive path splits on the drive-letter colon, so "C:\Tools"
    // arrives as "C" plus "\Tools". Drop both pieces.
    if (/^[A-Za-z]$/.test(entry) && next !== undefined && (next.includes('\\') || next.startsWith('\\') || next.startsWith('/'))) {
      index += 1;
      continue;
    }
    if (!entry || entry.startsWith('/mnt/') || entry.includes('\\') || /^[A-Za-z]:/.test(entry)) continue;
    kept.push(entry);
  }
  return kept.join(':');
}

function locationIsWindowsBacked(candidate, mounts) {
  if (/^[A-Za-z]:[\\/]/.test(candidate) || candidate.includes('\\')) return true;
  const resolved = resolveTargetWithoutCreating(candidate);
  const mount = mountForPath(mounts, resolved);
  if (!mount) throw new PlatformError('WSL_STORAGE_UNKNOWN', `Could not identify the filesystem for ${candidate}; refusing to use it as managed storage.`);
  return isWindowsBackedMount(mount);
}

export function sanitizeWslProcessEnvironment(env, { mountInfoText } = {}) {
  const next = { ...env };
  for (const key of WSL_REDIRECT_KEYS) delete next[key];
  if (typeof next.PATH === 'string') next.PATH = stripWindowsPathEntries(next.PATH);
  const needsMounts = next.SPEECHCRAFT_SETUP_DATA_DIR || next.XDG_DATA_HOME;
  let mounts = null;
  const loadMounts = () => {
    if (mounts) return mounts;
    let contents = mountInfoText;
    if (contents === undefined) {
      try { contents = fs.readFileSync('/proc/self/mountinfo', 'utf8'); }
      catch { throw new PlatformError('WSL_STORAGE_UNKNOWN', 'WSL was detected, but mount information is unavailable; refusing managed writes.'); }
    }
    mounts = parseMountInfo(contents);
    return mounts;
  };
  if (needsMounts) loadMounts();
  if (next.SPEECHCRAFT_SETUP_DATA_DIR && locationIsWindowsBacked(next.SPEECHCRAFT_SETUP_DATA_DIR, mounts)) {
    throw new PlatformError('WSL_WINDOWS_MOUNT', 'SPEECHCRAFT_SETUP_DATA_DIR is on a Windows-mounted filesystem. Choose a directory on the Ubuntu Linux filesystem, for example under your Linux home directory.');
  }
  if (next.XDG_DATA_HOME && locationIsWindowsBacked(next.XDG_DATA_HOME, mounts)) delete next.XDG_DATA_HOME;
  return next;
}

export function assertExistingManagedFiles(environment, files, options = {}) {
  for (const file of files) {
    if (!fs.existsSync(file)) continue;
    const info = fs.lstatSync(file);
    if (info.isSymbolicLink() || !info.isFile()) {
      throw new PlatformError('STORAGE_PATH_UNSAFE', 'Managed SpeechCraft files must be regular files on the Linux filesystem, not symbolic links.');
    }
    validateManagedStorageTargets(environment, [path.dirname(fs.realpathSync(file))], options);
  }
}

export function validateManagedStorageTargets(environment, directories, options = {}) {
  return assertWslStorage(environment, directories.map(resolveTargetWithoutCreating), options);
}

export function validateManagedStorage(environment, directories, options = {}) {
  const resolved = directories.map(resolveManagedDirectory);
  return assertWslStorage(environment, resolved, options);
}

export function inspectHost(options = {}) {
  const environment = hostEnvironment(options);
  const managedDirectories = options.directories || [];
  if (managedDirectories.length) validateManagedStorage(environment, managedDirectories, options);
  return environment;
}
