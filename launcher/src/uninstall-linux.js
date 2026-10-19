// Linux per-user uninstall. Deletes only verified application files under
// ~/.local/share/speechcraft/app and the owned ~/.local/bin/speechcraft
// wrapper. User data stays unless --purge names a real directory inside that
// tree. Symlinks are never followed. On Linux the running Node process keeps
// its inode if this file is unlinked, so an installed copy can remove itself.
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { defaultDataDirectory } from '../../setup-controller/src/paths.js';

export const INSTALL_LOCK_NAME = '.speechcraft-install.lock';
export const LOCK_WAIT_MS = 45_000;
export const PATH_BEGIN = '# >>> SpeechCraft user command PATH >>>';
export const PATH_END = '# <<< SpeechCraft user command PATH <<<';
export const PATH_BLOCK = `${PATH_BEGIN}\ncase ":$PATH:" in *:"$HOME/.local/bin:"*) ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac\nexport PATH\n${PATH_END}\n`;
const RELEASE_NAME = /^speechcraft-[A-Za-z0-9][A-Za-z0-9.-]{0,80}-linux-x86_64$/;
const PURGE_NAMES = ['data', 'setup-controller', 'models'];
const RELEASE_ENTRY_LIMIT = 50_000;

export class UninstallError extends Error {
  constructor(message) {
    super(message);
    this.name = 'UninstallError';
  }
}

export function wrapperScript(appBase) {
  return `#!/bin/sh\n# SpeechCraft per-user command wrapper\nexec "${appBase}/current/bin/speechcraft" "$@"\n`;
}

function isInside(child, parent) {
  const relative = path.relative(parent, child);
  return relative === '' || (!relative.startsWith('..') && !path.isAbsolute(relative));
}

function writeLine(stdout, line) {
  stdout.write(`${line}\n`);
}

function lstatOrNull(target) {
  try { return fs.lstatSync(target); }
  catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

function assertRealOwnedDir(target, uid, label) {
  const info = lstatOrNull(target);
  if (!info) return null;
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new UninstallError(`${label} is not a real directory: ${target}. Nothing was removed.`);
  }
  if (info.uid !== uid) throw new UninstallError(`${label} is not owned by the current user: ${target}. Nothing was removed.`);
  return info;
}

function resolveHome(home, uid) {
  const requested = path.resolve(home);
  let resolved;
  try { resolved = fs.realpathSync(requested); }
  catch (error) {
    if (error.code === 'ENOENT') throw new UninstallError(`Home directory does not exist: ${requested}. Nothing was removed.`);
    throw error;
  }
  const real = lstatOrNull(resolved);
  if (!real || real.isSymbolicLink() || !real.isDirectory() || real.uid !== uid) {
    throw new UninstallError(`Home directory is not a real directory owned by the current user: ${resolved}. Nothing was removed.`);
  }
  return resolved;
}

function walkOwnedChain(home, parts, uid) {
  let current = home;
  for (const part of parts) {
    current = path.join(current, part);
    const info = assertRealOwnedDir(current, uid, 'Installation path');
    if (!info) return { path: current, exists: false };
  }
  return { path: current, exists: true };
}

function readNoFollow(target) {
  const fd = fs.openSync(target, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  try { return fs.readFileSync(fd); }
  finally { fs.closeSync(fd); }
}

function inspectCommand(commandPath, appBase, uid) {
  const info = lstatOrNull(commandPath);
  if (!info) return null;
  if (info.uid !== uid) throw new UninstallError(`Refusing to remove ${commandPath}; it is not owned by the current user. Nothing was removed.`);
  if (info.isSymbolicLink()) {
    const linkText = fs.readlinkSync(commandPath);
    const lexical = path.resolve(path.dirname(commandPath), linkText);
    if (!isInside(lexical, appBase)) {
      throw new UninstallError(`Refusing to replace unrelated command: ${commandPath}. Nothing was removed.`);
    }
    return { path: commandPath, dev: info.dev, ino: info.ino, kind: 'symlink', linkText };
  }
  if (!info.isFile()) throw new UninstallError(`Refusing to remove ${commandPath}; it is not the SpeechCraft command wrapper. Nothing was removed.`);
  if (info.size > 4096) throw new UninstallError(`Refusing to replace unrelated command: ${commandPath}. Nothing was removed.`);
  const bytes = readNoFollow(commandPath);
  if (!bytes.equals(Buffer.from(wrapperScript(appBase)))) {
    throw new UninstallError(`Refusing to replace unrelated command: ${commandPath}. Nothing was removed.`);
  }
  return { path: commandPath, dev: info.dev, ino: info.ino, kind: 'file', mode: info.mode, bytes };
}

function releaseNameFromLink(linkText, releasesDir) {
  if (typeof linkText !== 'string' || linkText.includes('\0') || linkText.includes('\\')) return null;
  if (path.isAbsolute(linkText)) {
    const name = path.basename(linkText);
    if (linkText === path.join(releasesDir, name) && RELEASE_NAME.test(name)) return name;
    return null;
  }
  const parts = linkText.split('/');
  if (parts.length === 2 && parts[0] === 'releases' && RELEASE_NAME.test(parts[1]) && !parts[1].includes('..')) return parts[1];
  return null;
}

function manifestVersion(releasePath, uid) {
  const manifestPath = path.join(releasePath, 'application-release-manifest.json');
  const info = lstatOrNull(manifestPath);
  if (!info || info.isSymbolicLink() || !info.isFile() || info.uid !== uid || info.size > 1024 * 1024) return null;
  let manifest;
  try { manifest = JSON.parse(readNoFollow(manifestPath).toString('utf8')); }
  catch { return null; }
  if (!manifest || manifest.schema_version !== 1 || manifest.platform !== 'linux-x86_64' || manifest.application !== 'SpeechCraft') return null;
  if (typeof manifest.version !== 'string' || !manifest.version) return null;
  return manifest.version;
}

function listReleases(releasesDir, uid) {
  const verified = [];
  const leftovers = [];
  if (!lstatOrNull(releasesDir)) return { verified, leftovers };
  for (const name of fs.readdirSync(releasesDir)) {
    if (name === INSTALL_LOCK_NAME) continue;
    const releasePath = path.join(releasesDir, name);
    const info = fs.lstatSync(releasePath);
    if (!RELEASE_NAME.test(name) || info.isSymbolicLink() || !info.isDirectory() || info.uid !== uid) {
      leftovers.push(releasePath);
      continue;
    }
    const version = manifestVersion(releasePath, uid);
    const expected = name.slice('speechcraft-'.length, -'-linux-x86_64'.length);
    if (version !== expected) {
      leftovers.push(releasePath);
      continue;
    }
    verified.push({ path: releasePath, dev: info.dev, ino: info.ino, name });
  }
  verified.sort((left, right) => left.name.localeCompare(right.name));
  leftovers.sort();
  return { verified, leftovers };
}

function inspectCurrent(appBase, releasesDir, verified) {
  const current = path.join(appBase, 'current');
  const info = lstatOrNull(current);
  if (!info) return null;
  if (!info.isSymbolicLink()) {
    throw new UninstallError(`Active application pointer is not a symlink: ${current}. Nothing was removed.`);
  }
  const linkText = fs.readlinkSync(current);
  const releaseName = releaseNameFromLink(linkText, releasesDir);
  const target = releaseName ? path.join(releasesDir, releaseName) : null;
  if (!releaseName || path.resolve(appBase, linkText) !== target) {
    throw new UninstallError(`Active application pointer escapes the versioned release directory: ${current} -> ${linkText}. Nothing was removed.`);
  }
  if (!verified.some((release) => release.path === target)) {
    throw new UninstallError(`Active application pointer does not name a verified SpeechCraft release: ${current} -> ${linkText}. Nothing was removed.`);
  }
  return { path: current, dev: info.dev, ino: info.ino, linkText, target, releaseName };
}

function inspectProfile(profile, uid) {
  const info = lstatOrNull(profile);
  if (!info) return { path: profile, status: 'absent', action: `no SpeechCraft block in ${profile}` };
  if (info.isSymbolicLink() || !info.isFile() || info.uid !== uid) {
    return { path: profile, status: 'unsafe', action: `leaving ${profile} unchanged because it is not a regular user-owned file` };
  }
  let text;
  try { text = readNoFollow(profile).toString('utf8'); }
  catch {
    return { path: profile, status: 'unsafe', action: `leaving ${profile} unchanged because it could not be read safely` };
  }
  const removed = removePathBlock(text);
  if (removed.status === 'removed') {
    return { path: profile, status: 'removed', mode: info.mode, dev: info.dev, ino: info.ino, next: removed.text, action: `removing the exact SpeechCraft block from ${profile}` };
  }
  if (removed.status === 'absent') return { path: profile, status: 'absent', action: `no SpeechCraft block in ${profile}` };
  return { path: profile, status: removed.status, action: `leaving ${profile} unchanged because the SpeechCraft PATH block was edited or repeated` };
}

export function removePathBlock(text) {
  if (!text.includes(PATH_BEGIN)) return { status: 'absent', text };
  const start = text.indexOf(PATH_BLOCK);
  if (start === -1 || text.indexOf(PATH_BLOCK, start + PATH_BLOCK.length) !== -1) {
    return { status: text.indexOf(PATH_BEGIN) !== text.lastIndexOf(PATH_BEGIN) ? 'duplicate' : 'modified', text };
  }
  return { status: 'removed', text: text.slice(0, start) + text.slice(start + PATH_BLOCK.length) };
}

function activeDataDirectory({ dataDirectory, env, home, wsl }) {
  if (dataDirectory) return { path: path.resolve(dataDirectory), explicit: true };
  if (env.SPEECHCRAFT_SETUP_DATA_DIR) return { path: path.resolve(env.SPEECHCRAFT_SETUP_DATA_DIR), explicit: true };
  return { path: defaultDataDirectory(env, 'linux', home, { wsl: Boolean(wsl) }), explicit: false };
}

function directChildren(directory) {
  const info = lstatOrNull(directory);
  if (!info) return [];
  return fs.readdirSync(directory).map((name) => path.join(directory, name));
}

function classifyData(share, shareExists, active, uid) {
  const managed = [];
  const unsafe = [];
  const other = [];
  if (shareExists) {
    for (const name of PURGE_NAMES) {
      const candidate = path.join(share, name);
      const info = lstatOrNull(candidate);
      if (!info) continue;
      if (info.isSymbolicLink() || !info.isDirectory() || info.uid !== uid) unsafe.push(candidate);
      else managed.push(candidate);
    }
    for (const child of directChildren(share)) {
      const name = path.basename(child);
      if (name === 'app' || name === INSTALL_LOCK_NAME || PURGE_NAMES.includes(name)) continue;
      other.push(child);
    }
  }
  other.sort();
  unsafe.sort();
  return { managed, unsafe, other, active };
}

function purgeCovers(activePath, managed) {
  return managed.some((candidate) => isInside(activePath, candidate));
}

function missingAllowlisted(activePath, share) {
  return PURGE_NAMES.some((name) => isInside(activePath, path.join(share, name)));
}

export function inspectInstall({ home, env = {}, uid, dataDirectory = null, wsl = false, purge = false } = {}) {
  const resolvedHome = resolveHome(home, uid);
  const appBase = path.join(resolvedHome, '.local', 'share', 'speechcraft', 'app');
  const localDir = walkOwnedChain(resolvedHome, ['.local'], uid);
  const shareParent = localDir.exists ? walkOwnedChain(resolvedHome, ['.local', 'share'], uid) : { exists: false };
  const share = path.join(resolvedHome, '.local', 'share', 'speechcraft');
  const shareInfo = shareParent.exists ? assertRealOwnedDir(share, uid, 'SpeechCraft directory') : null;
  const binDir = localDir.exists ? walkOwnedChain(resolvedHome, ['.local', 'bin'], uid) : { path: path.join(resolvedHome, '.local', 'bin'), exists: false };
  const command = binDir.exists ? inspectCommand(path.join(binDir.path, 'speechcraft'), appBase, uid) : null;
  const appInfo = shareInfo ? assertRealOwnedDir(appBase, uid, 'Application root') : null;
  const releasesDir = path.join(appBase, 'releases');
  let verified = [];
  let leftovers = [];
  if (appInfo) {
    const releasesInfo = assertRealOwnedDir(releasesDir, uid, 'Release directory');
    if (releasesInfo) ({ verified, leftovers } = listReleases(releasesDir, uid));
    else leftovers.push(...directChildren(appBase).filter((child) => path.basename(child) !== 'current' && path.basename(child) !== 'releases'));
  }
  const current = appInfo ? inspectCurrent(appBase, releasesDir, verified) : null;
  const active = activeDataDirectory({ dataDirectory, env, home: resolvedHome, wsl });
  const data = classifyData(share, Boolean(shareInfo), active, uid);
  if (purge) assertPurgeAllowed(active, share, data);
  const preservedPaths = [...data.managed, ...data.other];
  if (!preservedPaths.includes(active.path)) preservedPaths.push(active.path);
  const profile = inspectProfile(path.join(resolvedHome, '.profile'), uid);
  const installed = Boolean(command || current || verified.length);
  return {
    home: resolvedHome, share, appBase, releasesDir, command, current, releases: verified, leftovers,
    profile, active, purgePaths: data.managed, preservedPaths, unsafeData: data.unsafe,
    installed, purge,
  };
}

function assertPurgeAllowed(active, share, data) {
  if (!isInside(active.path, share)) {
    throw new UninstallError(`Refusing --purge because the active data directory is outside ${share}: ${active.path}. Caches and projects outside that tree are not removed. No files were removed.`);
  }
  const info = lstatOrNull(active.path);
  if (info && (info.isSymbolicLink() || !info.isDirectory() || !purgeCovers(active.path, data.managed))) {
    throw new UninstallError(`Refusing --purge because the active data directory is not a verified real directory inside ${share}: ${active.path}. No files were removed.`);
  }
  if (!info && !purgeCovers(active.path, data.managed) && !missingAllowlisted(active.path, share)) {
    throw new UninstallError(`Refusing --purge because the active data directory is not a verified managed directory under ${share}: ${active.path}. No files were removed.`);
  }
}

export function formatPlan(plan) {
  const lines = ['SpeechCraft uninstall plan:'];
  lines.push(`Command: ${plan.command ? plan.command.path : '(not installed)'}`);
  lines.push(`Application root: ${plan.appBase}`);
  lines.push(`Active release: ${plan.current ? plan.current.target : '(none)'}`);
  lines.push('Releases to remove:');
  if (plan.releases.length === 0) lines.push('- (none)');
  for (const release of plan.releases) lines.push(`- ${release.path}`);
  lines.push(plan.purge ? 'Data to permanently delete:' : 'Preserved data:');
  const dataLines = plan.purge ? plan.purgePaths : plan.preservedPaths;
  if (dataLines.length === 0) lines.push('- (none present)');
  for (const item of dataLines) lines.push(`- ${item}`);
  if (plan.purge) {
    const stillKept = plan.preservedPaths.filter((item) => !plan.purgePaths.includes(item) && !purgeCovers(item, plan.purgePaths));
    if (stillKept.length) {
      lines.push('Still preserved:');
      for (const item of stillKept) lines.push(`- ${item}`);
    }
  }
  if (plan.leftovers.length || plan.unsafeData.length) {
    lines.push('Left in place:');
    for (const item of [...plan.leftovers, ...plan.unsafeData]) lines.push(`- ${item}`);
  }
  lines.push(`PATH block: ${plan.profile.action}`);
  if (plan.purge) {
    lines.push('Purge deletes the verified managed directories listed above, including controller state, managed runtimes, caches stored there, and models. This cannot be undone.');
  } else {
    lines.push('This removes the application command and versioned releases. Project audio, datasets, exports, models, and controller state stay in place.');
  }
  return lines.join('\n');
}

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) {
    if (error.code === 'ESRCH') return false;
    if (error.code === 'EPERM') return true;
    throw error;
  }
}

function lockIsLive(lockDir) {
  const info = lstatOrNull(lockDir);
  if (!info) return false;
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new UninstallError('SpeechCraft install lock is not a real directory. Nothing was removed.');
  }
  const pidPath = path.join(lockDir, 'pid');
  const pidInfo = lstatOrNull(pidPath);
  if (!pidInfo) return Date.now() - info.mtimeMs < 2_000;
  if (pidInfo.isSymbolicLink() || !pidInfo.isFile()) {
    throw new UninstallError('SpeechCraft install lock identity is not a regular file. Nothing was removed.');
  }
  const text = readNoFollow(pidPath).toString('ascii').trim();
  if (!/^[0-9]+$/.test(text)) return false;
  const pid = Number(text);
  if (pid === process.pid) return true;
  return pidAlive(pid);
}

function removeStaleLock(lockDir) {
  const pidPath = path.join(lockDir, 'pid');
  const pidInfo = lstatOrNull(pidPath);
  if (pidInfo && !pidInfo.isSymbolicLink()) fs.unlinkSync(pidPath);
  try { fs.rmdirSync(lockDir); } catch { /* the owner may have replaced it */ }
}

export async function acquireInstallLock(share, { timeoutMs = LOCK_WAIT_MS, uid } = {}) {
  assertRealOwnedDir(share, uid, 'SpeechCraft directory');
  const lockDir = path.join(share, INSTALL_LOCK_NAME);
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      fs.mkdirSync(lockDir, { mode: 0o700 });
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      if (lockIsLive(lockDir)) {
        if (Date.now() >= deadline) {
          throw new UninstallError('Another SpeechCraft install or uninstall holds the install lock. Nothing was removed. Retry after it finishes.');
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
        continue;
      }
      removeStaleLock(lockDir);
      continue;
    }
    const pidPath = path.join(lockDir, 'pid');
    try {
      const fd = fs.openSync(pidPath, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      try { fs.writeFileSync(fd, `${process.pid}\n`); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
    } catch (error) {
      removeStaleLock(lockDir);
      throw error;
    }
    return lockDir;
  }
}

export function releaseInstallLock(lockDir) {
  if (!lockDir) return;
  const info = lstatOrNull(lockDir);
  if (!info || info.isSymbolicLink() || !info.isDirectory()) return;
  const pidPath = path.join(lockDir, 'pid');
  try {
    const text = readNoFollow(pidPath).toString('ascii').trim();
    if (Number(text) !== process.pid) return;
    fs.unlinkSync(pidPath);
    fs.rmdirSync(lockDir);
  } catch { /* a lost lock is left for the next owner */ }
}

function assertSameInode(target, record, label) {
  const info = fs.lstatSync(target);
  if (info.dev !== record.dev || info.ino !== record.ino) {
    throw new UninstallError(`${label} changed at ${target} before it could be removed. Uninstall stopped before continuing.`);
  }
  return info;
}

function assertDeletable(target, invocationRoot, releases) {
  if (!invocationRoot) return;
  const root = path.resolve(invocationRoot);
  const candidate = path.resolve(target);
  if (releases.some((release) => isInside(root, release.path))) return;
  if (candidate === root || isInside(root, candidate)) {
    throw new UninstallError(`Refusing to remove the running source checkout at ${root}. This command only removes the per-user application under ~/.local/share/speechcraft/app. Nothing was removed.`);
  }
}

function removeOwnedTree(root, uid, { entryLimit = RELEASE_ENTRY_LIMIT, rootDev = null } = {}) {
  const info = fs.lstatSync(root);
  if (info.isSymbolicLink()) throw new UninstallError(`Refusing to follow symlink ${root}.`);
  if (info.uid !== uid) throw new UninstallError(`Refusing to remove ${root}; it is not owned by the current user.`);
  const device = rootDev ?? info.dev;
  if (info.dev !== device) throw new UninstallError(`Refusing to cross a filesystem boundary at ${root}.`);
  if (info.isDirectory()) {
    fs.chmodSync(root, 0o700);
    const names = fs.readdirSync(root);
    if (names.length > entryLimit) throw new UninstallError(`Refusing to remove ${root}; it contains more entries than a SpeechCraft release.`);
    let seen = names.length;
    for (const name of names) {
      if (!name || name.includes('/') || name.includes('\0')) throw new UninstallError(`Refusing to remove an unsafe path entry in ${root}.`);
      const child = path.join(root, name);
      if (!isInside(child, root)) throw new UninstallError(`Refusing to remove ${child}; it escapes ${root}.`);
      const childInfo = fs.lstatSync(child);
      if (childInfo.isSymbolicLink()) {
        fs.unlinkSync(child);
        continue;
      }
      if (childInfo.isDirectory()) {
        seen += removeOwnedTree(child, uid, { entryLimit, rootDev: device });
        if (seen > entryLimit) throw new UninstallError(`Refusing to remove ${root}; it contains more entries than a SpeechCraft release.`);
      } else if (childInfo.isFile()) {
        fs.chmodSync(child, childInfo.mode | 0o200);
        fs.unlinkSync(child);
      } else {
        throw new UninstallError(`Refusing to remove special file ${child}.`);
      }
    }
    fs.rmdirSync(root);
    return seen;
  }
  if (info.isFile()) {
    fs.chmodSync(root, info.mode | 0o200);
    fs.unlinkSync(root);
    return 1;
  }
  throw new UninstallError(`Refusing to remove special file ${root}.`);
}

function restoreCommand(record) {
  if (lstatOrNull(record.path)) return;
  const temporary = `${record.path}.restore-${process.pid}`;
  if (record.kind === 'symlink') fs.symlinkSync(record.linkText, temporary);
  else {
    const fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
    try { fs.writeFileSync(fd, record.bytes); fs.fsyncSync(fd); }
    finally { fs.closeSync(fd); }
    fs.chmodSync(temporary, record.mode & 0o777);
  }
  fs.renameSync(temporary, record.path);
}

function restoreCurrent(record) {
  if (lstatOrNull(record.path) || !lstatOrNull(record.target)) return;
  const temporary = `${record.path}.restore-${process.pid}`;
  fs.symlinkSync(record.linkText, temporary);
  fs.renameSync(temporary, record.path);
}

function removeIfEmpty(directory, uid) {
  const info = lstatOrNull(directory);
  if (!info || info.isSymbolicLink() || !info.isDirectory() || info.uid !== uid) return false;
  if (fs.readdirSync(directory).length) return false;
  fs.rmdirSync(directory);
  return true;
}

function writeProfile(record) {
  const current = fs.lstatSync(record.path);
  if (current.isSymbolicLink() || !current.isFile() || current.dev !== record.dev || current.ino !== record.ino) {
    throw new UninstallError(`PATH block was left in place because ${record.path} changed.`);
  }
  const temporary = path.join(path.dirname(record.path), `.speechcraft-profile-${process.pid}-${crypto.randomBytes(4).toString('hex')}`);
  const fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, record.mode & 0o777);
  try { fs.writeFileSync(fd, record.next); fs.fsyncSync(fd); }
  finally { fs.closeSync(fd); }
  const again = fs.lstatSync(record.path);
  if (again.isSymbolicLink() || again.dev !== record.dev || again.ino !== record.ino) {
    fs.unlinkSync(temporary);
    throw new UninstallError(`PATH block was left in place because ${record.path} changed.`);
  }
  fs.renameSync(temporary, record.path);
}

function identities(plan) {
  return JSON.stringify({
    command: plan.command && [plan.command.dev, plan.command.ino, plan.command.kind],
    current: plan.current && [plan.current.dev, plan.current.ino, plan.current.linkText],
    releases: plan.releases.map((release) => [release.dev, release.ino, release.name]),
    purge: plan.purgePaths,
  });
}

function ask(stdin, stdout, prompt) {
  return new Promise((resolve) => {
    const rl = readline.createInterface({ input: stdin, output: stdout });
    rl.question(prompt, (answer) => { rl.close(); resolve(answer); });
  });
}

async function confirmed(options, kind, prompt) {
  if (options.yes) return true;
  if (typeof options.confirm === 'function') return options.confirm(kind, prompt) === true;
  if (options.isTTY !== true) {
    throw new UninstallError('Refusing to uninstall without --yes when input is not a terminal. Re-run speechcraft uninstall --yes to remove the application and keep user data.');
  }
  const answer = (await ask(options.stdin, options.stdout, prompt)).trim();
  if (kind === 'purge') return answer === 'purge';
  return answer.toLowerCase() === 'y' || answer.toLowerCase() === 'yes';
}

function guardCheckout(plan, invocationRoot) {
  for (const target of [plan.appBase, plan.command?.path, ...plan.releases.map((release) => release.path), ...plan.purgePaths]) {
    if (target) assertDeletable(target, invocationRoot, plan.releases);
  }
}

export async function runLinuxUninstall(options = {}) {
  const platform = options.platform || process.platform;
  if (platform !== 'linux') {
    throw new UninstallError('speechcraft uninstall is only implemented for Linux per-user installs. macOS and Windows uninstall are not available in this version.');
  }
  const stdout = options.stdout || process.stdout;
  const uid = options.uid ?? process.getuid();
  const env = options.env || {};
  const plan = inspectInstall({
    home: options.home || os.homedir(), env, uid, dataDirectory: options.dataDirectory || null,
    wsl: options.wsl === true, purge: options.purge === true,
  });
  guardCheckout(plan, options.invocationRoot);
  const purgeWork = options.purge === true && plan.purgePaths.length > 0;
  if (!plan.installed && !purgeWork) {
    writeLine(stdout, 'SpeechCraft is not installed for this user. Nothing was removed.');
    return { command: 'uninstall', removed: false };
  }
  writeLine(stdout, formatPlan(plan));
  if (!await confirmed(options, 'uninstall', 'Uninstall SpeechCraft application files? [y/N] ')) {
    writeLine(stdout, 'Uninstall cancelled. Nothing was removed.');
    return { command: 'uninstall', removed: false, cancelled: true };
  }
  if (options.purge === true && !await confirmed(options, 'purge', 'Type "purge" to permanently delete the managed data directories listed above: ')) {
    writeLine(stdout, 'Uninstall cancelled. Nothing was removed.');
    return { command: 'uninstall', removed: false, cancelled: true };
  }
  writeLine(stdout, options.yes ? (options.purge ? 'Confirmed with --purge --yes.' : 'Confirmed with --yes.') : 'Confirmed.');
  const shareInfo = lstatOrNull(plan.share);
  const lock = shareInfo ? await acquireInstallLock(plan.share, { timeoutMs: options.lockTimeoutMs ?? LOCK_WAIT_MS, uid }) : null;
  let commandRemoved = false;
  let currentRemoved = false;
  let releasesStarted = false;
  const removed = [];
  try {
    const again = inspectInstall({
      home: plan.home, env, uid, dataDirectory: plan.active.explicit ? plan.active.path : null,
      wsl: options.wsl === true, purge: options.purge === true,
    });
    guardCheckout(again, options.invocationRoot);
    if (identities(plan) !== identities(again)) {
      throw new UninstallError('The installation changed while waiting for the install lock. Nothing was removed. Retry uninstall.');
    }
    if (typeof options.stopServices !== 'function') {
      throw new UninstallError('Internal error: uninstall was not given the service shutdown check. Nothing was removed.');
    }
    try {
      const stopped = await options.stopServices(plan.active.path);
      if (!stopped || stopped.stopped !== true) {
        throw new UninstallError('SpeechCraft shutdown was not confirmed. Uninstall aborted; no application files were removed.');
      }
    } catch (error) {
      if (error instanceof UninstallError) throw error;
      throw new UninstallError(`${error.message} Uninstall aborted; no application files were removed.`);
    }
    const cwd = options.cwd || process.cwd();
    const deletionRoots = [again.appBase, ...again.purgePaths];
    if (deletionRoots.some((rootPath) => isInside(cwd, rootPath))) process.chdir(os.tmpdir());
    try {
      if (again.command) {
        assertSameInode(again.command.path, again.command, 'Command');
        fs.unlinkSync(again.command.path);
        commandRemoved = true;
        removed.push(again.command.path);
      }
      if (again.current) {
        assertSameInode(again.current.path, again.current, 'Application pointer');
        fs.unlinkSync(again.current.path);
        currentRemoved = true;
        removed.push(again.current.path);
      }
      releasesStarted = again.releases.length > 0;
      for (const release of again.releases) {
        if (options.hooks?.beforeReleaseRemoval) await options.hooks.beforeReleaseRemoval(release);
        assertSameInode(release.path, release, 'Release');
        removeOwnedTree(release.path, uid);
        removed.push(release.path);
      }
      if (removeIfEmpty(again.releasesDir, uid)) removed.push(again.releasesDir);
      if (removeIfEmpty(again.appBase, uid)) removed.push(again.appBase);
    } catch (error) {
      const notes = [];
      if (commandRemoved) {
        try { restoreCommand(again.command); notes.push(releasesStarted ? 'the command wrapper was restored so uninstall can be retried' : 'the command wrapper was restored'); }
        catch { notes.push('the command wrapper could not be restored'); }
      }
      if (currentRemoved && !releasesStarted) {
        try { restoreCurrent(again.current); notes.push('the application pointer was restored'); }
        catch { notes.push('the application pointer could not be restored'); }
      } else if (currentRemoved && releasesStarted) {
        notes.push('the application pointer was removed so it does not point at a partial release');
      }
      throw new UninstallError(`${error.message} ${notes.join('; ')}. User data was not removed.`.replace(/\s+/g, ' ').trim());
    }
    if (again.profile.status === 'removed') {
      try { writeProfile(again.profile); }
      catch (error) {
        throw new UninstallError(`Application files were removed, but ${error.message} User data was not removed. Retry uninstall to finish the PATH update.`);
      }
    }
    const purged = [];
    if (options.purge === true) {
      for (const candidate of again.purgePaths) {
        const info = lstatOrNull(candidate);
        if (!info) continue;
        if (info.isSymbolicLink() || !info.isDirectory() || info.uid !== uid || !isInside(candidate, again.share) || path.dirname(candidate) !== again.share) {
          throw new UninstallError(`Application files were removed, but purge refused ${candidate}. Retry after inspecting that path.`);
        }
        try { removeOwnedTree(candidate, uid, { entryLimit: Number.MAX_SAFE_INTEGER }); }
        catch (error) {
          throw new UninstallError(`Application files were removed, but purge could not finish ${candidate}: ${error.message}`);
        }
        purged.push(candidate);
        removed.push(candidate);
      }
    }
    writeLine(stdout, 'SpeechCraft application uninstalled.');
    for (const item of removed) writeLine(stdout, `Removed: ${item}`);
    const preserved = again.preservedPaths.filter((item) => !purged.includes(item) && lstatOrNull(item));
    writeLine(stdout, 'Preserved data:');
    if (preserved.length === 0) writeLine(stdout, '- (none present)');
    for (const item of preserved) writeLine(stdout, `- ${item}`);
    if (again.profile.status === 'removed') writeLine(stdout, `PATH block: removed from ${again.profile.path}`);
    else writeLine(stdout, `PATH block: ${again.profile.action}`);
    return { command: 'uninstall', removed: true, purged, preserved, removedPaths: removed };
  } finally {
    releaseInstallLock(lock);
  }
}
