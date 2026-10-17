import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

export const WINDOWS_PACKAGE_FILES = Object.freeze([
  'speechcraft.exe',
  'install-windows.ps1',
  'install-linux.py',
  'installation.schema.json',
  'windows-release.json',
  'WINDOWS.md',
  'accept-windows.ps1',
]);

export function sha256File(filename) {
  return crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
}

export function validateWindowsPackage(directory) {
  const missing = WINDOWS_PACKAGE_FILES.filter((name) => !fs.existsSync(path.join(directory, name)));
  if (missing.length) return { ok: false, missing };
  const exe = fs.statSync(path.join(directory, 'speechcraft.exe'));
  if (!exe.isFile() || exe.size < 1024) return { ok: false, missing: ['speechcraft.exe is not a built executable'] };
  const release = JSON.parse(fs.readFileSync(path.join(directory, 'windows-release.json'), 'utf8'));
  const problems = [];
  if (release.schema_version !== 1) problems.push('schema_version');
  if (release.platform !== 'windows-x64') problems.push('platform');
  if (release.launcher?.target !== 'windows/amd64') problems.push('launcher target');
  if (!/^[a-f0-9]{40}$/.test(release.source_revision || '')) problems.push('source_revision');
  if (!/^[a-f0-9]{64}$/.test(release.launcher?.sha256 || '')) problems.push('launcher sha256');
  if (release.launcher.sha256 !== sha256File(path.join(directory, 'speechcraft.exe'))) problems.push('launcher sha256 mismatch');
  if (release.signing?.status !== 'unsigned_development_build') problems.push('signing status');
  if (release.signing?.smartscreen !== 'not_established') problems.push('smartscreen status');
  if (release.linux_payload?.platform !== 'linux-x86_64') problems.push('linux platform');
  if (!['external_verified_dependency', 'included'].includes(release.linux_payload?.status)) problems.push('linux payload status');
  const schema = JSON.parse(fs.readFileSync(path.join(directory, 'installation.schema.json'), 'utf8'));
  if (schema.properties?.guest_id?.const !== 'ubuntu' || schema.properties?.wsl_version?.const !== 2) problems.push('installation schema');
  const installer = fs.readFileSync(path.join(directory, 'install-windows.ps1'), 'utf8');
  if (!installer.includes('--windows-install') || installer.includes('cmd.exe')) problems.push('installer entrypoint');
  const acceptance = fs.readFileSync(path.join(directory, 'accept-windows.ps1'), 'utf8');
  if (!acceptance.includes('PENDING_NATIVE_WINDOWS')) problems.push('acceptance marker');
  return { ok: problems.length === 0, missing, problems, release };
}
