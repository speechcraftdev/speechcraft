#!/usr/bin/env node
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const frontendRoot = path.join(repositoryRoot, 'frontend');
const source = path.join(frontendRoot, '.next/standalone/frontend');
const staticSource = path.join(frontendRoot, '.next/static');
const publicSource = path.join(frontendRoot, 'public');
const outputArgument = process.argv.indexOf('--output');
if (outputArgument < 0 || !process.argv[outputArgument + 1]) throw new Error('Usage: package-frontend.mjs --output ABSOLUTE_DIRECTORY');
const output = path.resolve(process.argv[outputArgument + 1]);
if (!path.isAbsolute(output) || output === path.parse(output).root || output === repositoryRoot || output === frontendRoot) {
  throw new Error('Choose an absolute staging directory that is not the source checkout or frontend directory.');
}
const typecheck = spawnSync('npm', ['run', 'typecheck'], { cwd: frontendRoot, stdio: 'inherit', shell: false });
if (typecheck.error || typecheck.status !== 0) throw new Error('Strict frontend typecheck failed; refusing to stage a release frontend.');
if (!fs.existsSync(path.join(source, 'server.js')) || !fs.statSync(path.join(source, 'server.js')).isFile()) {
  throw new Error('Next.js standalone output is missing. Run `npm run build` first.');
}
if (!fs.existsSync(staticSource) || !fs.statSync(staticSource).isDirectory()) throw new Error('Next.js static assets are missing.');
if (fs.existsSync(output)) throw new Error('Release staging output already exists; choose a new empty path.');
fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
const temporary = `${output}.staging-${process.pid}`;
try {
  fs.mkdirSync(temporary, { mode: 0o700 });
  const frontendOutput = path.join(temporary, 'frontend');
  // Next's standalone tree contains only traced production dependencies. Keep
  // that tree intact; filtering node_modules here silently breaks runtime
  // route handlers. The post-copy walk rejects symlinks rather than following
  // them outside the release root.
  fs.cpSync(source, frontendOutput, { recursive: true, errorOnExist: true, verbatimSymlinks: true });
  fs.mkdirSync(path.join(frontendOutput, '.next'), { recursive: true });
  fs.cpSync(staticSource, path.join(frontendOutput, '.next/static'), { recursive: true, errorOnExist: true });
  if (fs.existsSync(publicSource)) fs.cpSync(publicSource, path.join(frontendOutput, 'public'), { recursive: true, errorOnExist: true });
  const files = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`Standalone package contains an unexpected symlink: ${filename}`);
      if (entry.isDirectory()) visit(filename);
      else if (entry.isFile()) {
        const bytes = fs.readFileSync(filename);
        files.push({ path: path.relative(temporary, filename).split(path.sep).join('/'), size_bytes: bytes.length,
          sha256: createHash('sha256').update(bytes).digest('hex') });
      } else throw new Error(`Standalone package contains an unsupported filesystem entry: ${filename}`);
    }
  };
  visit(temporary);
  const buildId = fs.readFileSync(path.join(frontendRoot, '.next/BUILD_ID'), 'utf8').trim();
  fs.writeFileSync(path.join(temporary, 'frontend-release-manifest.json'), JSON.stringify({
    schema_version: 1, next_build_id: buildId, typecheck: 'passed', files,
  }, null, 2), { flag: 'wx', mode: 0o600 });
  fs.renameSync(temporary, output);
  console.log(`Staged standalone Next.js frontend at ${output} (${files.length} verified files; build ${buildId}).`);
} catch (error) {
  fs.rmSync(temporary, { recursive: true, force: true });
  throw error;
}
