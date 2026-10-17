import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

function liveWsl2() {
  try {
    return /microsoft-standard-WSL2/i.test(fs.readFileSync('/proc/sys/kernel/osrelease', 'utf8'));
  } catch {
    return false;
  }
}

export function defaultDataDirectory(env = process.env, platform = process.platform, home = os.homedir(), options = {}) {
  if (env.SPEECHCRAFT_SETUP_DATA_DIR) return path.resolve(env.SPEECHCRAFT_SETUP_DATA_DIR);
  if (platform === 'darwin') {
    return path.join(home, 'Library', 'Application Support', 'SpeechCraft', 'setup-controller');
  }
  // On WSL2, ignore XDG_DATA_HOME. Windows interop can import it from a
  // Windows profile and would otherwise redirect managed data onto DrvFS.
  const wsl = options.wsl === true || (options.wsl !== false && platform === 'linux' && liveWsl2());
  const base = wsl ? path.join(home, '.local', 'share') : (env.XDG_DATA_HOME || path.join(home, '.local', 'share'));
  return path.join(base, 'speechcraft', 'setup-controller');
}
