import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { defaultDataDirectory } from '../../setup-controller/src/paths.js';

const PATH_BEGIN = '# >>> SpeechCraft user command PATH >>>';
const PATH_END = '# <<< SpeechCraft user command PATH <<<';
const PATH_BLOCK = `${PATH_BEGIN}\ncase ":$PATH:" in *:"$HOME/Library/Application Support/SpeechCraft/bin:"*) ;; *) PATH="$HOME/Library/Application Support/SpeechCraft/bin:$PATH" ;; esac\nexport PATH\n${PATH_END}\n`;
const WRAPPER_TEXT = '#!/bin/sh\nexec "${HOME}/Library/Application Support/SpeechCraft/app/current/bin/speechcraft" "$@"\n';

function lstatOrNull(target) {
  try { return fs.lstatSync(target); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}

function realOwnedDirectory(target, uid) {
  const info = lstatOrNull(target);
  if (!info) return false;
  if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== uid || fs.realpathSync(target) !== path.resolve(target)) {
    throw new Error(`Refusing to remove an unsafe or unowned SpeechCraft directory: ${target}`);
  }
  return true;
}

function safeCommand(command, uid) {
  const info = lstatOrNull(command);
  if (!info) return false;
  if (info.isSymbolicLink() || !info.isFile() || info.uid !== uid || info.size > 4096) {
    throw new Error(`Refusing to remove an unrelated command at ${command}.`);
  }
  const contents = fs.readFileSync(command, 'utf8');
  if (contents !== WRAPPER_TEXT) throw new Error(`Refusing to remove an unrelated command at ${command}.`);
  return true;
}

function removePathBlock(profile, uid) {
  const info = lstatOrNull(profile);
  if (!info || !info.isFile() || info.isSymbolicLink() || info.uid !== uid || info.size > 1_000_000) return;
  const text = fs.readFileSync(profile, 'utf8');
  const start = text.indexOf(PATH_BLOCK);
  if (start < 0 || text.indexOf(PATH_BLOCK, start + PATH_BLOCK.length) >= 0
      || text.indexOf(PATH_BEGIN) !== text.lastIndexOf(PATH_BEGIN)
      || text.indexOf(PATH_END) !== text.lastIndexOf(PATH_END)) return;
  fs.writeFileSync(profile, text.slice(0, start) + text.slice(start + PATH_BLOCK.length), { mode: info.mode & 0o777 });
}

function ask(stdin, stdout, prompt) {
  const rl = readline.createInterface({ input: stdin, output: stdout });
  return new Promise((resolve) => rl.question(prompt, (answer) => { rl.close(); resolve(answer.trim().toLowerCase()); }));
}

export async function runMacUninstall({ home = os.homedir(), env = process.env, yes = false, purge = false,
  dataDirectory = null, stdin = process.stdin, stdout = process.stdout, stderr = process.stderr,
  isTTY = stdin.isTTY === true, stopServices = async () => ({ stopped: true }) } = {}) {
  if (process.platform !== 'darwin' && env.SPEECHCRAFT_INSTALL_PLATFORM_TEST !== 'darwin') throw new Error('Mac uninstaller can run only on macOS.');
  const uid = typeof process.getuid === 'function' ? process.getuid() : null;
  if (uid === null) throw new Error('Could not verify current user ownership; uninstallation aborted.');
  const resolvedHome = fs.realpathSync(path.resolve(home));
  const base = path.join(resolvedHome, 'Library', 'Application Support', 'SpeechCraft');
  const app = path.join(base, 'app');
  const command = path.join(base, 'bin', 'speechcraft');
  const managed = dataDirectory ? path.resolve(dataDirectory) : defaultDataDirectory(env, 'darwin', resolvedHome);
  const knownManaged = path.join(base, 'setup-controller');
  if (purge && managed !== knownManaged) throw new Error('--purge is limited to the default SpeechCraft managed data directory.');
  if (!yes) {
    if (!isTTY) throw new Error('Uninstall requires confirmation; pass --yes for noninteractive removal.');
    const answer = await ask(stdin, stdout, purge
      ? `Remove SpeechCraft and all data in ${knownManaged}? [y/N] `
      : 'Remove the SpeechCraft application and keep projects, models, logs, and exports? [y/N] ');
    if (answer !== 'y' && answer !== 'yes') { stdout.write('Uninstall cancelled.\n'); return { cancelled: true }; }
  }
  const appExists = realOwnedDirectory(app, uid);
  const managedExists = purge && realOwnedDirectory(knownManaged, uid);
  if (appExists || managedExists) await stopServices(managed);
  if (appExists) {
    const binExists = realOwnedDirectory(path.join(base, 'bin'), uid);
    const commandExists = binExists && safeCommand(command, uid);
    const releases = path.join(app, 'releases');
    if (realOwnedDirectory(releases, uid)) {
      for (const name of fs.readdirSync(releases)) {
        if (!/^speechcraft-[0-9][A-Za-z0-9.-]{0,80}-macos-arm64$/.test(name)) continue;
        const release = path.join(releases, name);
        const info = fs.lstatSync(release);
        if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== uid) throw new Error(`Refusing to remove unsafe release: ${release}`);
        const manifestPath = path.join(release, 'application-release-manifest.json');
        const manifestInfo = lstatOrNull(manifestPath);
        if (!manifestInfo || manifestInfo.isSymbolicLink() || !manifestInfo.isFile() || manifestInfo.uid !== uid) throw new Error(`Refusing to remove unverified release: ${release}`);
        const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
        if (manifest.platform !== 'macos-arm64' || manifest.application !== 'SpeechCraft' || manifest.version !== name.slice('speechcraft-'.length, -'-macos-arm64'.length)) throw new Error(`Refusing to remove unverified release: ${release}`);
        fs.rmSync(release, { recursive: true, force: false });
      }
    }
    const current = path.join(app, 'current');
    const currentInfo = lstatOrNull(current);
    if (currentInfo) {
      if (!currentInfo.isSymbolicLink() || !fs.readlinkSync(current).startsWith('releases/speechcraft-')) throw new Error('Refusing to remove an unsafe active release pointer.');
      fs.unlinkSync(current);
    }
    if (commandExists) fs.unlinkSync(command);
    removePathBlock(path.join(resolvedHome, '.zprofile'), uid);
    // Keep the data root and all project/model content by default.
    if (purge && realOwnedDirectory(knownManaged, uid)) fs.rmSync(knownManaged, { recursive: true, force: false });
    try { fs.rmdirSync(releases); } catch (error) { if (!['ENOTEMPTY', 'ENOENT'].includes(error.code)) throw error; }
    try { fs.rmdirSync(app); } catch (error) { if (!['ENOTEMPTY', 'ENOENT'].includes(error.code)) throw error; }
    try { fs.rmdirSync(path.join(base, 'bin')); } catch (error) { if (!['ENOTEMPTY', 'ENOENT'].includes(error.code)) throw error; }
    stdout.write(purge ? 'SpeechCraft application and managed data removed.\n' : 'SpeechCraft application removed; projects, models, logs, and exports were kept.\n');
    return { removed: true, purged: purge };
  }
  if (purge && realOwnedDirectory(knownManaged, uid)) fs.rmSync(knownManaged, { recursive: true, force: false });
  stdout.write('SpeechCraft application is already absent.\n');
  return { removed: true, purged: purge };
}
