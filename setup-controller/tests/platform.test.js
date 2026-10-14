import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { hostEnvironment, parseMountInfo, validateManagedStorage } from '../src/platform.js';

const mount = (point, fsType, source = 'none') =>
  `42 1 0:1 / ${point.replaceAll(' ', '\\040')} rw,relatime - ${fsType} ${source} rw`;

test('platform profiles are based on the actual host and WSL is detected independently', () => {
  assert.deepEqual(hostEnvironment({ platform: 'linux', arch: 'x64', kernelRelease: '6.6.87.2-microsoft-standard-WSL2', kernelVersion: 'Linux version' }),
    { os: 'linux', architecture: 'x86_64', profile: 'linux-x86_64', wsl: true });
  assert.deepEqual(hostEnvironment({ platform: 'darwin', arch: 'arm64' }),
    { os: 'macos', architecture: 'arm64', profile: 'macos-arm64', wsl: false });
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

test('storage validation refuses a symlinked managed root', () => {
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-policy-link-'));
  const target = path.join(parent, 'target');
  fs.mkdirSync(target);
  const alias = path.join(parent, 'alias');
  fs.symlinkSync(target, alias);
  assert.throws(() => validateManagedStorage({ wsl: true }, [alias], { mountInfoText: mount(parent, 'ext4') }),
    (error) => error.code === 'STORAGE_PATH_UNSAFE');
});
