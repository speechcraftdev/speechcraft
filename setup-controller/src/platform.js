import fs from 'node:fs';
import path from 'node:path';

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
    const wsl = isWslKernel({ platform, release, version });
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
    const identity = `${mount.fileSystem} ${mount.source} ${mount.superOptions.join(' ')}`;
    if (/^(9p|drvfs|ntfs|ntfs3|fuseblk)$/i.test(mount.fileSystem)
        || /drvfs|aname=drvfs|path=[A-Z]:\\/i.test(identity)) {
      throw new PlatformError('WSL_WINDOWS_MOUNT', 'SpeechCraft managed data, runtimes, environments, caches, and temporary files must be stored on the WSL Linux filesystem, not a Windows-mounted drive. Move the SpeechCraft data directory to WSL ext4.');
    }
  }
  return resolved;
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
