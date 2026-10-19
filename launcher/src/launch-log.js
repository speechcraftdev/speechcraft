import fs from 'node:fs';
import path from 'node:path';

export const LAUNCHER_LOG_LIMIT_BYTES = 256 * 1024;

const SECRET_ASSIGNMENT = /((?:authorization|cookie|csrf|token|secret|password)["']?\s*[:=]\s*["']?)[^\s"',}]+/gi;
const BEARER_VALUE = /\bBearer\s+[A-Za-z0-9._~+/-]{8,}/g;

export function redactLaunchText(text, secrets = []) {
  let result = String(text);
  const values = [...secrets].filter((value) => typeof value === 'string' && value.length >= 8);
  values.sort((left, right) => right.length - left.length);
  for (const secret of values) result = result.split(secret).join('[redacted]');
  return result.replace(BEARER_VALUE, 'Bearer [redacted]').replace(SECRET_ASSIGNMENT, '$1[redacted]');
}

export function isRoutineChildStdout(line) {
  const text = String(line).trim();
  if (text.startsWith('{"event":"listening"') || text.startsWith('{"event":"shutdown"')) return true;
  if (text.startsWith('▲ Next.js') || text.startsWith('- Local:') || text.startsWith('- Network:')) return true;
  if (text.startsWith('✓ Ready') || text.startsWith('✓ Starting') || text.startsWith('✓ Compiled') || text.startsWith('○ Compiling')) return true;
  return false;
}

export function classifyChildStreamLine(stream, line) {
  const text = String(line ?? '');
  if (!text.trim()) return 'drop';
  if (stream === 'stderr') return 'show';
  if (isRoutineChildStdout(text)) return 'log';
  return 'show';
}

export function launchOutputMode({ verbose = false, env = {}, stdoutIsTTY = false } = {}) {
  const level = String(env.SPEECHCRAFT_LOG_LEVEL || '').toLowerCase();
  const verboseEnabled = verbose === true || level === 'verbose' || level === 'debug';
  return { verbose: verboseEnabled, emitReadyEvent: verboseEnabled || stdoutIsTTY !== true };
}

export function formatRemoteAccessGuidance(url) {
  return [
    `No local browser was opened. SpeechCraft is serving ${url} on this machine (127.0.0.1 only).`,
    'Open a browser on the computer where you use SSH. Leave SpeechCraft running here.',
    'From that computer, start a tunnel and keep it open. Example:',
    '  ssh -N -L 3002:127.0.0.1:3002 cozytent',
    'cozytent is only an example SSH alias. Use your own alias or user@host.',
    `Then visit ${url} in the browser on that computer.`,
    'Leave the services bound to 127.0.0.1.',
  ].join('\n');
}

export function formatLaunchAccess({ url, opened, env = {} }) {
  if (opened) return `SpeechCraft asked this desktop to open ${url}.`;
  const remote = Boolean(env.SSH_CONNECTION || env.SSH_CLIENT || env.SSH_TTY);
  const headless = !env.DISPLAY && !env.WAYLAND_DISPLAY;
  const lead = remote
    ? 'This is an SSH session, so SpeechCraft did not open a browser on the server.'
    : headless
      ? 'No display was found, so SpeechCraft did not open a browser here.'
      : 'SpeechCraft could not open a desktop browser.';
  return `${lead}\n${formatRemoteAccessGuidance(url)}`;
}

export function assertLauncherLogPath(dataDirectory, logFile) {
  const root = fs.realpathSync(dataDirectory);
  const resolved = path.resolve(logFile);
  if (path.basename(resolved) !== 'launcher.log') throw new Error('Unexpected SpeechCraft log name.');
  const parent = fs.realpathSync(path.dirname(resolved));
  if (parent !== root) throw new Error('SpeechCraft log must stay inside the data directory.');
  return resolved;
}

export function createLauncherLog(directory, { limit = LAUNCHER_LOG_LIMIT_BYTES } = {}) {
  const file = path.join(directory, 'launcher.log');
  const rotated = `${file}.1`;
  const state = { fd: null, size: 0 };
  const openAppend = () => {
    const flags = fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_APPEND | (fs.constants.O_NOFOLLOW || 0);
    let fd;
    try { fd = fs.openSync(file, flags, 0o600); }
    catch (error) {
      if (error.code === 'ELOOP') throw new Error('SpeechCraft log file must be a regular file, not a symlink.');
      throw error;
    }
    try {
      fs.fchmodSync(fd, 0o600);
      const info = fs.fstatSync(fd);
      if (!info.isFile()) throw new Error('SpeechCraft log file must be a regular file.');
      if (typeof process.getuid === 'function' && info.uid !== process.getuid()) {
        throw new Error('SpeechCraft log file must be owned by the current user.');
      }
      state.fd = fd;
      state.size = info.size;
      fd = null;
    } finally { if (fd !== null) fs.closeSync(fd); }
  };
  const rotate = () => {
    if (state.fd !== null) { fs.closeSync(state.fd); state.fd = null; }
    fs.rmSync(rotated, { force: true });
    try {
      fs.renameSync(file, rotated);
      fs.chmodSync(rotated, 0o600);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    openAppend();
  };
  try {
    const existing = fs.lstatSync(file);
    if (existing.isSymbolicLink()) throw new Error('SpeechCraft log file must be a regular file, not a symlink.');
    if (existing.isFile() && existing.size >= limit) {
      fs.rmSync(rotated, { force: true });
      if (existing.size <= limit * 2) {
        fs.renameSync(file, rotated);
        fs.chmodSync(rotated, 0o600);
      } else fs.rmSync(file, { force: true });
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (state.fd === null) openAppend();
  return {
    path: file,
    write(text) {
      const payload = String(text).endsWith('\n') ? String(text) : `${text}\n`;
      const bytes = Buffer.byteLength(payload);
      if (state.size > 0 && state.size + bytes > limit) rotate();
      fs.writeSync(state.fd, payload);
      state.size += bytes;
    },
    close() {
      if (state.fd !== null) { fs.closeSync(state.fd); state.fd = null; }
    },
  };
}

function readSecretFile(file) {
  try {
    const value = fs.readFileSync(file, 'utf8').trim();
    if (value.length >= 8 && value.length <= 4096) return value;
  } catch { /* a missing secret file has nothing to redact */ }
  return null;
}

export async function runLauncherLogHelper() {
  const dataDirectory = process.env.SPEECHCRAFT_LAUNCHER_DATA_DIR || '';
  const requestedLog = process.env.SPEECHCRAFT_LAUNCHER_LOG_FILE || '';
  const logFile = assertLauncherLogPath(dataDirectory, requestedLog);
  const verbose = process.env.SPEECHCRAFT_LAUNCHER_LOG_VERBOSE === '1';
  const log = createLauncherLog(path.dirname(logFile));
  let cached = { at: 0, secrets: [] };
  const secretsNow = () => {
    const now = Date.now();
    if (now - cached.at < 200) return cached.secrets;
    const secrets = ['bootstrap.token', 'frontend.identity']
      .map((name) => readSecretFile(path.join(dataDirectory, name)))
      .filter(Boolean);
    cached = { at: now, secrets };
    return secrets;
  };
  const emit = (line) => {
    const safe = redactLaunchText(line, secretsNow()).slice(0, 4000);
    const action = classifyChildStreamLine('stdout', safe);
    if (action === 'drop') return;
    log.write(safe);
    if (action === 'show' || verbose) {
      try { process.stdout.write(`${safe}\n`); }
      catch (error) { if (error?.code !== 'EPIPE') throw error; }
    }
  };
  process.stdout.on('error', (error) => { if (error.code !== 'EPIPE') throw error; });
  process.stdin.setEncoding('utf8');
  let buffer = '';
  process.stdin.on('data', (chunk) => {
    buffer += chunk;
    if (buffer.length > 1024 * 1024) buffer = buffer.slice(-8192);
    const parts = buffer.split('\n');
    buffer = parts.pop() ?? '';
    for (const part of parts) emit(part);
  });
  await new Promise((resolve) => {
    process.stdin.on('end', () => {
      if (buffer) emit(buffer);
      resolve();
    });
  });
  log.close();
}
