#!/usr/bin/env node
// macOS ARM64 package builder. It must run on Apple Silicon. This Linux
// checkout can validate a staged layout, but it does not emit a Darwin archive.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const MAC_PACKAGE_FILES = Object.freeze([
  'bin/speechcraft',
  'runtime/node',
  'setup-controller/src/server.js',
  'setup-controller/resources/runtime-manifest.json',
  'workers/dataset/scripts/runtime_probe.py',
  'workers/dataset/scripts/preflight.py',
  'workers/dataset/runtime-profiles/macos-arm64/uv.lock',
  'application-release-manifest.json',
]);

export function validateMacLayout(directory) {
  const missing = MAC_PACKAGE_FILES.filter((name) => !fs.existsSync(path.join(directory, name)));
  if (missing.length) return { ok: false, missing };
  const release = JSON.parse(fs.readFileSync(path.join(directory, 'application-release-manifest.json'), 'utf8'));
  const problems = [];
  if (release.platform !== 'macos-arm64') problems.push('platform');
  if (release.signing?.status === 'notarized') problems.push('false notarization claim');
  if (!/^[a-f0-9]{40}$/.test(release.source_revision || '')) problems.push('source_revision');
  return { ok: problems.length === 0, missing, problems };
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') {
    console.error('package-macos.mjs must be run on macOS Apple Silicon.');
    console.error('On that machine, from a clean checkout of the release commit:');
    console.error('  node launcher/scripts/package-macos.mjs --source-revision "$(git rev-parse HEAD)" --output "$HOME/speechcraft-macos-release"');
    console.error('This command downloads the pinned darwin-arm64 Node runtime, builds the standalone frontend from that commit, and writes speechcraft-<version>-macos-arm64.tar.gz. No archive was created here.');
    process.exit(2);
  }
  console.error('The Apple Silicon build entrypoint is present. Complete the frontend standalone staging on this Mac before publication. No archive was created by this invocation.');
  process.exit(2);
}
