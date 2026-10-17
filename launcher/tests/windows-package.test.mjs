import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { sha256File, validateWindowsPackage } from '../scripts/windows-package-lib.mjs';

test('package validation accepts a complete unsigned Windows bundle and rejects a missing executable', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-win-pkg-'));
  const exe = Buffer.alloc(2048, 1);
  fs.writeFileSync(path.join(root, 'speechcraft.exe'), exe);
  fs.writeFileSync(path.join(root, 'install-windows.ps1'), '& $exe --windows-install --linux-archive $Archive\n');
  fs.writeFileSync(path.join(root, 'install-linux.py'), 'print("installer")\n');
  fs.writeFileSync(path.join(root, 'installation.schema.json'), JSON.stringify({ properties: { guest_id: { const: 'ubuntu' }, wsl_version: { const: 2 } } }));
  fs.writeFileSync(path.join(root, 'WINDOWS.md'), 'Windows\n');
  fs.writeFileSync(path.join(root, 'accept-windows.ps1'), 'PENDING_NATIVE_WINDOWS\n');
  const release = {
    schema_version: 1, platform: 'windows-x64', source_revision: 'a'.repeat(40),
    launcher: { target: 'windows/amd64', sha256: sha256File(path.join(root, 'speechcraft.exe')) },
    signing: { status: 'unsigned_development_build', smartscreen: 'not_established' },
    linux_payload: { status: 'external_verified_dependency', platform: 'linux-x86_64' },
  };
  fs.writeFileSync(path.join(root, 'windows-release.json'), JSON.stringify(release));
  assert.equal(validateWindowsPackage(root).ok, true);
  fs.rmSync(path.join(root, 'speechcraft.exe'));
  const missing = validateWindowsPackage(root);
  assert.equal(missing.ok, false);
  assert.ok(missing.missing.includes('speechcraft.exe'));
});

test('package validation rejects a signed claim and a non-Linux payload', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-win-pkg-bad-'));
  fs.writeFileSync(path.join(root, 'speechcraft.exe'), Buffer.alloc(2048, 2));
  fs.writeFileSync(path.join(root, 'install-windows.ps1'), '& $exe --windows-install\n');
  fs.writeFileSync(path.join(root, 'install-linux.py'), 'print("installer")\n');
  fs.writeFileSync(path.join(root, 'installation.schema.json'), JSON.stringify({ properties: { guest_id: { const: 'ubuntu' }, wsl_version: { const: 2 } } }));
  fs.writeFileSync(path.join(root, 'WINDOWS.md'), 'Windows\n');
  fs.writeFileSync(path.join(root, 'accept-windows.ps1'), 'PENDING_NATIVE_WINDOWS\n');
  fs.writeFileSync(path.join(root, 'windows-release.json'), JSON.stringify({
    schema_version: 1, platform: 'windows-x64', source_revision: 'a'.repeat(40),
    launcher: { target: 'windows/amd64', sha256: sha256File(path.join(root, 'speechcraft.exe')) },
    signing: { status: 'signed', smartscreen: 'trusted' },
    linux_payload: { status: 'included', platform: 'windows-x64' },
  }));
  const result = validateWindowsPackage(root);
  assert.equal(result.ok, false);
  assert.ok(result.problems.includes('signing status'));
  assert.ok(result.problems.includes('linux platform'));
});
