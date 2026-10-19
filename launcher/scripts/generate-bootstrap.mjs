#!/usr/bin/env node
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const args = process.argv.slice(2);
const value = (flag) => { const index = args.indexOf(flag); return index < 0 ? null : args[index + 1] || null; };
const output = value('--output'); const tag = value('--tag'); const revision = value('--source-revision');
const requestedAssetBaseUrl = value('--asset-base-url');
const linuxOnly = args.includes('--linux-only');
if (!output || !/^v[0-9]+\.[0-9]+\.[0-9]+(?:-internal\.[1-9][0-9]*)?$/.test(tag || '') || !/^[a-f0-9]{40}$/.test(revision || '')) {
  throw new Error('Usage: node launcher/scripts/generate-bootstrap.mjs --tag vX.Y.Z[-internal.N] --source-revision 40-character-commit --output DIRECTORY [--asset-base-url HTTPS-URL|LOOPBACK-HTTP-URL] [--linux-only]');
}
const assetBaseUrl = requestedAssetBaseUrl || `https://github.com/speechcraftdev/speechcraft/releases/download/${tag}`;
let parsed;
try { parsed = new URL(assetBaseUrl); } catch { throw new Error('Release asset base URL must be a valid absolute URL.'); }
const loopbackHttp = parsed.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(parsed.hostname) && parsed.port !== '';
if (parsed.protocol !== 'https:' && !loopbackHttp) throw new Error('Release asset base URL must use HTTPS; HTTP is allowed only with an explicit loopback port for private staging.');
if (parsed.username || parsed.password || parsed.search || parsed.hash || !/^[A-Za-z0-9._~:/-]+$/.test(assetBaseUrl) || assetBaseUrl.endsWith('/')) throw new Error('Release asset base URL contains unsafe URL components.');
const run = spawnSync('git', ['rev-parse', `${revision}^{commit}`], { cwd: root, encoding: 'utf8', shell: false });
if (run.status !== 0 || run.stdout.trim() !== revision) throw new Error('The requested source revision did not resolve exactly.');
const versionResult = spawnSync('git', ['show', `${revision}:frontend/package.json`], { cwd: root, encoding: 'utf8', shell: false });
if (versionResult.status !== 0) throw new Error('Could not read application version from the pinned commit.');
const version = JSON.parse(versionResult.stdout).version;
if (!/^\d+\.\d+\.\d+$/.test(version) || (tag !== `v${version}` && !tag.startsWith(`v${version}-internal.`))) throw new Error('Release tag and application version do not match.');
const destination = path.resolve(output);
const sha = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const readJson = (file) => JSON.parse(fs.readFileSync(file, 'utf8'));
const platforms = {
  linux: { suffix: 'linux-x86_64', installer: ['install-linux.py', 'install-linux.sh'] },
  ...(linuxOnly ? {} : { macos: { suffix: 'macos-arm64', installer: ['install-macos.sh'] } }),
};
const pins = { RELEASE_TAG: tag, APP_VERSION: version, SOURCE_REVISION: revision, ASSET_BASE_URL: assetBaseUrl };
if (linuxOnly) for (const key of ['MACOS_ARCHIVE', 'MACOS_ARCHIVE_SHA256', 'MACOS_ARCHIVE_SIZE', 'MACOS_CHECKSUM_SHA256', 'MACOS_CHECKSUM_SIZE', 'MACOS_MANIFEST_SHA256', 'MACOS_MANIFEST_SIZE', 'INSTALL_MACOS_SH_SHA256', 'INSTALL_MACOS_SH_SIZE']) pins[key] = '';
for (const [key, platform] of Object.entries(platforms)) {
  const archives = fs.readdirSync(destination).filter((name) => name === `speechcraft-${version}-${platform.suffix}.tar.gz`);
  if (archives.length !== 1) throw new Error(`Output must contain the ${platform.suffix} archive for ${version}.`);
  const archive = archives[0]; const archivePath = path.join(destination, archive);
  const checksumPath = `${archivePath}.sha256`; const releasePath = `${archivePath}.release.json`;
  const files = [checksumPath, releasePath, ...platform.installer.map((name) => path.join(destination, name))];
  for (const file of files) if (!fs.existsSync(file) || !fs.lstatSync(file).isFile()) throw new Error(`Required ${platform.suffix} release file is missing: ${path.basename(file)}`);
  const manifest = readJson(releasePath); const archiveSha = sha(archivePath); const archiveSize = fs.statSync(archivePath).size;
  const checksumTokens = fs.readFileSync(checksumPath, 'utf8').trim().split(/\s+/);
  if (checksumTokens[0] !== archiveSha || checksumTokens.at(-1) !== archive) throw new Error(`${platform.suffix} checksum file is inconsistent.`);
  const expected = { schema_version: 1, archive, archive_sha256: archiveSha, archive_size_bytes: archiveSize, application_version: version, platform: platform.suffix, source_revision: revision };
  if (Object.entries(expected).some(([name, value]) => manifest[name] !== value)) throw new Error(`${platform.suffix} archive release metadata does not match the requested source and version.`);
  pins[`${key.toUpperCase()}_ARCHIVE`] = archive;
  pins[`${key.toUpperCase()}_ARCHIVE_SHA256`] = archiveSha;
  pins[`${key.toUpperCase()}_ARCHIVE_SIZE`] = String(archiveSize);
  pins[`${key.toUpperCase()}_CHECKSUM_SHA256`] = sha(checksumPath);
  pins[`${key.toUpperCase()}_CHECKSUM_SIZE`] = String(fs.statSync(checksumPath).size);
  pins[`${key.toUpperCase()}_MANIFEST_SHA256`] = sha(releasePath);
  pins[`${key.toUpperCase()}_MANIFEST_SIZE`] = String(fs.statSync(releasePath).size);
  for (const installer of platform.installer) {
    const file = path.join(destination, installer); const installerKey = installer.replaceAll('-', '_').replaceAll('.', '_').toUpperCase();
    pins[installerKey + '_SHA256'] = sha(file); pins[installerKey + '_SIZE'] = String(fs.statSync(file).size);
  }
}
let template = fs.readFileSync(path.join(root, 'launcher/installer/install.sh.in'), 'utf8');
for (const [key, pin] of Object.entries(pins)) template = template.replaceAll(`@@${key}@@`, pin);
if (/@@[A-Z_]+@@/.test(template)) throw new Error('Bootstrap template contains an unfilled release pin.');
const installer = path.join(destination, 'install.sh');
fs.writeFileSync(installer, template, { flag: 'wx', mode: 0o755 });
console.log(linuxOnly
  ? `Generated Linux-only bootstrap for ${tag} from ${revision}; Mac dispatch is disabled for this legacy Linux release.`
  : `Generated shared bootstrap for ${tag} from ${revision}; Linux and macOS release identities match.`);
