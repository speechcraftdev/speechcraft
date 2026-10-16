#!/usr/bin/env node
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { pipeline } from 'node:stream/promises';
import { Readable } from 'node:stream';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const manifest = JSON.parse(fs.readFileSync(path.join(root, 'launcher/resources/node-runtime.json'), 'utf8'));
const outputArg = process.argv.indexOf('--output');
const platformArg = process.argv.indexOf('--platform');
const selected = platformArg >= 0 ? process.argv[platformArg + 1] : process.platform === 'darwin' ? 'macos-arm64' : 'linux-x86_64';
const artifact = manifest.artifacts[selected];
if (!artifact) throw new Error(`No bundled Node runtime is defined for ${selected}.`);
if (outputArg < 0 || !process.argv[outputArg + 1]) throw new Error('Usage: fetch-node-runtime.mjs --output ABSOLUTE_DIRECTORY [--platform linux-x86_64|macos-arm64]');
const output = path.resolve(process.argv[outputArg + 1]);
if (!path.isAbsolute(output) || output === path.parse(output).root) throw new Error('Output must be an absolute non-root directory.');
if (fs.existsSync(output)) throw new Error('Node runtime output already exists; choose a fresh staging directory.');
fs.mkdirSync(path.dirname(output), { recursive: true, mode: 0o700 });
const stage = fs.mkdtempSync(path.join(path.dirname(output), '.speechcraft-node-stage-'));
const archive = path.join(stage, artifact.file);

async function download() {
  const response = await fetch(artifact.url, { redirect: 'error', signal: AbortSignal.timeout(120_000) });
  if (!response.ok || !response.body || new URL(response.url).hostname !== 'nodejs.org') throw new Error('Could not acquire the pinned Node.js archive from nodejs.org.');
  const hash = createHash('sha256'); let size = 0;
  const meter = new (await import('node:stream')).Transform({ transform(chunk, _encoding, callback) {
    size += chunk.length;
    if (size > 200 * 1024 * 1024) return callback(new Error('Pinned Node archive exceeded the download size limit.'));
    hash.update(chunk); callback(null, chunk);
  } });
  await pipeline(Readable.fromWeb(response.body), meter, fs.createWriteStream(archive, { flags: 'wx', mode: 0o600 }));
  if (hash.digest('hex') !== artifact.sha256) throw new Error('Downloaded Node.js archive does not match the trusted SHA-256.');
}

function runTar(args, destination) {
  return new Promise((resolve, reject) => {
    const child = spawn('tar', args, { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
    const out = fs.createWriteStream(destination, { flags: 'wx', mode: 0o600 });
    let stderr = ''; let settled = false;
    const finish = (error) => { if (settled) return; settled = true; error ? reject(error) : resolve(); };
    child.stderr.setEncoding('utf8').on('data', (value) => { stderr = (stderr + value).slice(-2048); });
    child.stdout.pipe(out);
    out.once('error', finish);
    child.once('error', finish);
    child.once('close', (code) => finish(code === 0 ? null : new Error(`Could not extract the verified Node.js payload (${stderr.slice(0, 300)}).`)));
  });
}

try {
  await download();
  const top = artifact.file.replace(/\.tar\.(?:xz|gz)$/, '');
  const binary = path.join(stage, 'node');
  await runTar(artifact.archive === 'xz' ? ['-xOf', archive, `${top}/bin/node`] : ['-xOf', archive, `${top}/bin/node`], binary);
  const license = path.join(stage, 'LICENSE');
  await runTar(['-xOf', archive, `${top}/LICENSE`], license);
  const stats = fs.statSync(binary);
  if (!stats.isFile() || stats.size < 10_000_000 || stats.size > 150_000_000) throw new Error('Pinned Node.js executable has an implausible size.');
  fs.chmodSync(binary, 0o755);
  const runtimeManifest = { schema_version: 1, version: manifest.version, platform: selected, source: manifest.source,
    artifact: artifact.file, artifact_sha256: artifact.sha256, executable_sha256: createHash('sha256').update(fs.readFileSync(binary)).digest('hex'),
    licensed_notice: 'Node.js bundled distribution license is included in LICENSE.' };
  fs.writeFileSync(path.join(stage, 'runtime-manifest.json'), JSON.stringify(runtimeManifest, null, 2), { flag: 'wx', mode: 0o600 });
  if ((selected === 'linux-x86_64' && process.platform === 'linux' && process.arch === 'x64')
      || (selected === 'macos-arm64' && process.platform === 'darwin' && process.arch === 'arm64')) {
    await new Promise((resolve, reject) => {
      const child = spawn(binary, ['--version'], { shell: false, stdio: ['ignore', 'pipe', 'pipe'] });
      let stdout = ''; child.stdout.setEncoding('utf8').on('data', (value) => { stdout += value; });
      child.once('error', reject); child.once('close', (code) => code === 0 && stdout.trim() === `v${manifest.version}` ? resolve() : reject(new Error('Bundled Node runtime version verification failed.')));
    });
  }
  fs.mkdirSync(output, { recursive: false, mode: 0o700 });
  fs.renameSync(binary, path.join(output, 'node'));
  fs.renameSync(license, path.join(output, 'LICENSE'));
  fs.renameSync(path.join(stage, 'runtime-manifest.json'), path.join(output, 'runtime-manifest.json'));
  console.log(`Verified Node.js ${manifest.version} ${selected} runtime staged at ${output}.`);
} catch (error) {
  fs.rmSync(output, { recursive: true, force: true });
  throw error;
} finally {
  fs.rmSync(stage, { recursive: true, force: true });
}
