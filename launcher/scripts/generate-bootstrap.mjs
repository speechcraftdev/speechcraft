#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const args = process.argv.slice(2);
const value = (flag) => { const index = args.indexOf(flag); return index < 0 ? null : args[index + 1] || null; };
const output = value('--output');
const tag = value('--tag');
const revision = value('--source-revision');
if (!output || !/^v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][a-zA-Z0-9.-]+)?$/.test(tag || '') || !/^[a-f0-9]{40}$/.test(revision || '')) {
  throw new Error('Usage: node launcher/scripts/generate-bootstrap.mjs --tag vX.Y.Z --source-revision 40-character-commit --output DIRECTORY');
}
const destination = path.resolve(output);
const run = spawnSync('git', ['rev-parse', `${revision}^{commit}`], { cwd: root, encoding: 'utf8', shell: false });
if (run.status !== 0 || run.stdout.trim() !== revision) throw new Error('The requested source revision did not resolve exactly.');
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const sha = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const archiveReleaseFiles = fs.readdirSync(destination).filter((name) => name.endsWith('-linux-x86_64.tar.gz'));
if (archiveReleaseFiles.length !== 1) throw new Error('Output must contain exactly one Linux x86-64 application archive.');
const archive = archiveReleaseFiles[0];
const archivePath = path.join(destination, archive);
const checksum = `${archivePath}.sha256`;
const releaseManifest = `${archivePath}.release.json`;
const pythonInstaller = path.join(destination, 'install-linux.py');
const shellInstaller = path.join(destination, 'install-linux.sh');
for (const file of [checksum, releaseManifest, pythonInstaller, shellInstaller]) if (!fs.existsSync(file) || !fs.statSync(file).isFile()) throw new Error(`Required Linux release file is missing: ${path.basename(file)}`);
const manifest = readJson(releaseManifest);
const archiveSha = sha(archivePath);
const versionResult = spawnSync('git', ['show', `${revision}:frontend/package.json`], { cwd: root, encoding: 'utf8', shell: false });
if (versionResult.status !== 0) throw new Error('Could not read the application version from the pinned source commit.');
const sourceVersion = JSON.parse(versionResult.stdout).version;
if (!/^\d+\.\d+\.\d+$/.test(sourceVersion)) throw new Error('Pinned application version must be a plain numeric semantic version.');
if (tag !== `v${sourceVersion}`) throw new Error(`Release tag must match the pinned application version v${sourceVersion}.`);
if (archive !== `speechcraft-${sourceVersion}-linux-x86_64.tar.gz`) throw new Error('Linux archive filename does not match the pinned application version and platform.');
if (manifest.platform !== 'linux-x86_64' || manifest.source_revision !== revision || manifest.application_version !== sourceVersion
  || manifest.archive !== archive || manifest.archive_sha256 !== archiveSha || manifest.archive_size_bytes !== fs.statSync(archivePath).size) {
  throw new Error('Archive release metadata does not match requested source commit, application version, size, and SHA-256.');
}
const checksumTokens = fs.readFileSync(checksum, 'utf8').trim().split(/\s+/);
if (checksumTokens[0] !== archiveSha || checksumTokens.at(-1) !== archive) throw new Error('Adjacent archive checksum is inconsistent.');
const pins = {
  RELEASE_TAG: tag,
  APP_VERSION: sourceVersion,
  SOURCE_REVISION: revision,
  ARCHIVE: archive,
  ARCHIVE_SHA256: archiveSha,
  ARCHIVE_SIZE: String(fs.statSync(archivePath).size),
  CHECKSUM_SHA256: sha(checksum),
  MANIFEST_SHA256: sha(releaseManifest),
  INSTALLER_SHA256: sha(pythonInstaller),
  INSTALLER_SH_SHA256: sha(shellInstaller),
};
let template = fs.readFileSync(path.join(root, 'launcher/installer/install.sh.in'), 'utf8');
for (const [key, pin] of Object.entries(pins)) template = template.replaceAll(`@@${key}@@`, pin);
if (/@@[A-Z_]+@@/.test(template)) throw new Error('Bootstrap template contains an unfilled release pin.');
fs.writeFileSync(path.join(destination, 'install.sh'), template, { flag: 'wx', mode: 0o755 });
console.log(`Generated bootstrap for ${tag} from ${revision}`);
console.log(`Archive ${archive} SHA-256 ${archiveSha}`);
