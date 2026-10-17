import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { defaultDataDirectory } from '../src/paths.js';
import {
  assertCurrentWslGuest, compareDottedVersions, hostEnvironment, isWindowsBackedMount, managedStorageLocations,
  parseMountInfo, parseOsRelease, sanitizeWslProcessEnvironment, stripWindowsPathEntries, validateManagedStorage,
  validateManagedStorageTargets, MINIMUM_GLIBC, SUPPORTED_UBUNTU_VERSIONS,
} from '../src/platform.js';

const mount = (point, fsType, source = 'none') =>
  `42 1 0:1 / ${point.replaceAll(' ', '\\040')} rw,relatime - ${fsType} ${source} rw`;

test('platform profiles are based on the actual host and WSL is detected independently', () => {
  assert.deepEqual(hostEnvironment({ platform: 'linux', arch: 'x64', kernelRelease: '6.6.87.2-microsoft-standard-WSL2', kernelVersion: 'Linux version' }),
    { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true });
  assert.deepEqual(hostEnvironment({ platform: 'darwin', arch: 'arm64' }),
    { os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false });
  assert.throws(() => hostEnvironment({ platform: 'linux', arch: 'x64', kernelRelease: '4.4.0-microsoft', kernelVersion: 'Linux version' }),
    (error) => error.code === 'WSL_VERSION_UNSUPPORTED');
  assert.throws(() => hostEnvironment({ platform: 'win32', arch: 'x64' }), /does not support/);
});

test('WSL permits native Linux mounts and rejects a Windows mount reached through an ordinary path', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-wsl-policy-'));
  const linuxMount = parseMountInfo(mount(root, 'ext4', '/dev/sda'));
  assert.equal(linuxMount[0].fileSystem, 'ext4');
  assert.equal(validateManagedStorage({ wsl: true }, [root], { mountInfoText: mount(root, 'ext4', '/dev/sda') })[0], fs.realpathSync(root));
  assert.throws(() => validateManagedStorage({ wsl: true }, [root], {
    mountInfoText: `${mount('/', 'ext4', '/dev/sda')}\n${mount(root, '9p', 'C:\\')}`,
  }), (error) => error.code === 'WSL_WINDOWS_MOUNT');
  assert.throws(() => validateManagedStorage({ wsl: true }, [root], { mountInfoText: '' }),
    (error) => error.code === 'WSL_STORAGE_UNKNOWN');
});

test('native Linux and macOS do not receive WSL-only mount restrictions', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-native-policy-'));
  assert.equal(validateManagedStorage({ wsl: false }, [root], { mountInfoText: '' })[0], fs.realpathSync(root));
});

const ubuntu = (version) => `NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="${version}"\n`;
const ext4 = (point) => mount(point, 'ext4', '/dev/sda');

test('WSL guest checks require Ubuntu 22.04 or 24.04, x86-64, and glibc 2.28', () => {
  const host = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true };
  assert.deepEqual(SUPPORTED_UBUNTU_VERSIONS, ['22.04', '24.04']);
  assert.equal(MINIMUM_GLIBC, '2.28');
  assert.equal(assertCurrentWslGuest(host, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.39', freeBytes: 10, requiredBytes: 5, loopbackAvailable: true, processGroupAvailable: true }).version, '24.04');
  assert.equal(assertCurrentWslGuest(host, { osReleaseText: ubuntu('22.04'), glibcVersion: '2.35' }).glibc, '2.35');
  assert.equal(assertCurrentWslGuest({ wsl: false }), null);
  assert.equal(compareDottedVersions('2.35', '2.28'), 1);
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: ubuntu('20.04'), glibcVersion: '2.31' }),
    (error) => error.code === 'UBUNTU_RELEASE_UNSUPPORTED');
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: 'ID=debian\nVERSION_ID="12"\n', glibcVersion: '2.36' }),
    (error) => error.code === 'DISTRO_UNSUPPORTED');
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.17' }),
    (error) => error.code === 'GLIBC_INCOMPATIBLE');
  assert.throws(() => assertCurrentWslGuest({ ...host, architecture: 'arm64' }, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.39', architecture: 'arm64' }),
    (error) => error.code === 'ARCHITECTURE_UNSUPPORTED');
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.39', freeBytes: 1, requiredBytes: 5 }),
    (error) => error.code === 'STORAGE_CAPACITY_INSUFFICIENT');
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.39', loopbackAvailable: false }),
    (error) => error.code === 'LOOPBACK_UNAVAILABLE');
  assert.throws(() => assertCurrentWslGuest(host, { osReleaseText: ubuntu('24.04'), glibcVersion: '2.39', processGroupAvailable: false }),
    (error) => error.code === 'PROCESS_GROUP_UNAVAILABLE');
  assert.equal(parseOsRelease(ubuntu('22.04')).ID, 'ubuntu');
});

test('WSL storage policy distinguishes native ext4 from DrvFS, 9p, NTFS, and network mounts', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-wsl-fs-'));
  const models = path.join(root, 'models');
  const projects = path.join(root, 'application-data', 'projects');
  fs.mkdirSync(projects, { recursive: true });
  const native = `${ext4('/')}\n${ext4(root)}`;
  assert.equal(isWindowsBackedMount(parseMountInfo(mount(root, 'ext4', '/dev/sda'))[0]), false);
  for (const filesystem of ['9p', 'drvfs', 'ntfs', 'ntfs3', 'fuseblk', 'cifs', 'smbfs']) {
    assert.throws(() => validateManagedStorage({ wsl: true }, [root], { mountInfoText: mount(root, filesystem, 'drvfs') }),
      (error) => error.code === 'WSL_WINDOWS_MOUNT', filesystem);
  }
  const bind = `${ext4(root)}\n${mount(models, '9p', 'C:\\')}`;
  fs.mkdirSync(models);
  assert.throws(() => validateManagedStorageTargets({ wsl: true }, [models], { mountInfoText: bind }),
    (error) => error.code === 'WSL_WINDOWS_MOUNT');
  assert.equal(validateManagedStorageTargets({ wsl: true }, [projects], { mountInfoText: native })[0], fs.realpathSync(projects));
  const link = path.join(root, 'linked-models');
  fs.symlinkSync(path.join('/mnt/c/Users/test', 'models'), link);
  assert.throws(() => validateManagedStorage({ wsl: true }, [link], { mountInfoText: native }),
    (error) => error.code === 'STORAGE_PATH_UNSAFE');
});

test('WSL environment sanitizing drops Windows-backed cache overrides and keeps an explicit Linux data root', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-wsl-env-'));
  const mounts = `${ext4('/')}\n${ext4(root)}\n${mount('/mnt/c', '9p', 'C:\\')}`;
  const host = { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true };
  const sanitized = sanitizeWslProcessEnvironment({
    HF_HOME: '/mnt/c/Users/test/.cache/huggingface',
    TMPDIR: '/mnt/c/Users/test/Temp',
    UV_CACHE_DIR: '/mnt/c/cache/uv',
    XDG_DATA_HOME: '/mnt/c/Users/test/AppData',
    XDG_CACHE_HOME: '/mnt/c/Users/test/.cache',
    PATH: '/usr/bin:/mnt/c/Windows/System32:/home/test/.local/bin',
    SPEECHCRAFT_SETUP_DATA_DIR: root,
    LANG: 'C.UTF-8',
  }, { mountInfoText: mounts });
  assert.equal(sanitized.HF_HOME, undefined);
  assert.equal(sanitized.TMPDIR, undefined);
  assert.equal(sanitized.UV_CACHE_DIR, undefined);
  assert.equal(sanitized.XDG_DATA_HOME, undefined);
  assert.equal(sanitized.XDG_CACHE_HOME, undefined);
  assert.equal(sanitized.PATH, '/usr/bin:/home/test/.local/bin');
  assert.equal(sanitized.SPEECHCRAFT_SETUP_DATA_DIR, root);
  assert.equal(sanitized.LANG, 'C.UTF-8');
  assert.equal(stripWindowsPathEntries('C:\\Windows\\System32:/usr/bin'), '/usr/bin');
  assert.throws(() => sanitizeWslProcessEnvironment({ SPEECHCRAFT_SETUP_DATA_DIR: '/mnt/c/SpeechCraft' }, { mountInfoText: mounts }),
    (error) => error.code === 'WSL_WINDOWS_MOUNT');
  const home = path.join(root, 'home');
  fs.mkdirSync(home);
  assert.equal(defaultDataDirectory({ XDG_DATA_HOME: '/mnt/c/Users/test/share', SPEECHCRAFT_SETUP_DATA_DIR: '' }, 'linux', home, { wsl: true }),
    path.join(home, '.local', 'share', 'speechcraft', 'setup-controller'));
  assert.equal(defaultDataDirectory({ XDG_DATA_HOME: path.join(root, 'xdg') }, 'linux', home, { wsl: false }),
    path.join(root, 'xdg', 'speechcraft', 'setup-controller'));
  const locations = managedStorageLocations(root);
  assert.ok(locations.some((item) => item.endsWith(`${path.sep}models`)));
  assert.ok(locations.some((item) => item.endsWith(`${path.sep}sources`)));
  assert.ok(locations.some((item) => item.endsWith(`${path.sep}exports`)));
  assert.equal(validateManagedStorage(host, [path.join(root, 'models')], { mountInfoText: `${ext4(root)}` }).length, 1);
});

test('storage validation refuses a symlinked managed root', () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-policy-link-'));
  const target = path.join(parent, 'target');
  fs.mkdirSync(target);
  const alias = path.join(parent, 'alias');
  fs.symlinkSync(target, alias);
  assert.throws(() => validateManagedStorage({ wsl: true }, [alias], { mountInfoText: mount(parent, 'ext4') }),
    (error) => error.code === 'STORAGE_PATH_UNSAFE');
});
