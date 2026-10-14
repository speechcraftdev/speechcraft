import os from 'node:os';
import path from 'node:path';

export function defaultDataDirectory(env = process.env, platform = process.platform, home = os.homedir()) {
  if (env.SPEECHCRAFT_SETUP_DATA_DIR) return path.resolve(env.SPEECHCRAFT_SETUP_DATA_DIR);
  if (platform === 'darwin') {
    return path.join(home, 'Library', 'Application Support', 'SpeechCraft', 'setup-controller');
  }
  const base = env.XDG_DATA_HOME || path.join(home, '.local', 'share');
  return path.join(base, 'speechcraft', 'setup-controller');
}
