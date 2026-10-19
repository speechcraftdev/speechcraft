#!/bin/bash
set -euo pipefail

assets=${1:?usage: smoke-install-macos.sh CANDIDATE_ASSET_DIRECTORY SOURCE_REVISION RELEASE_TAG}
revision=${2:?missing source revision}
tag=${3:?missing release tag}
[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo 'This smoke test requires an Apple Silicon macOS runner.' >&2; exit 2; }
test "$(git rev-parse HEAD)" = "$revision"
root=$(mktemp -d "${RUNNER_TEMP:-/tmp}/speechcraft-macos-smoke.XXXXXXXX")
home="$root/home with spaces"
mkdir -p "$home" "$root/tmp"
server_pid=
app_pid=
failure_logs() {
  code=$?
  for log in "$root/launch.log" "$root/restart.log" "$root/stop.log"; do
    if [[ -f "$log" ]]; then echo "--- $log ---" >&2; cat "$log" >&2; fi
  done
  if [[ -n "${base:-}" && -f "$base/setup-controller/launcher.log" ]]; then
    echo "--- $base/setup-controller/launcher.log ---" >&2
    tail -n 200 "$base/setup-controller/launcher.log" >&2
  fi
  if [[ -n "${base:-}" && -d "$base/setup-controller/logs" ]]; then
    find "$base/setup-controller/logs" -type f -exec sh -c 'echo "--- $1 ---" >&2; tail -n 100 "$1" >&2' sh {} \; 2>/dev/null || true
  fi
  return "$code"
}
cleanup() {
  safe_to_remove=1
  if [[ -x "$home/Library/Application Support/SpeechCraft/bin/speechcraft" ]]; then
    if ! HOME="$home" "$home/Library/Application Support/SpeechCraft/bin/speechcraft" stop >/dev/null 2>&1; then
      echo 'Cleanup could not confirm owned services stopped; preserving the disposable home for runner teardown.' >&2
      safe_to_remove=0
    fi
  fi
  [[ -z "$server_pid" ]] || kill "$server_pid" >/dev/null 2>&1 || true
  if [[ $safe_to_remove == 1 ]]; then rm -rf "$root"; fi
}
trap cleanup EXIT
trap failure_logs ERR

node launcher/macos/serve-candidate.mjs "$assets" "$root/port" & server_pid=$!
for _ in $(seq 1 100); do [[ -s "$root/port" ]] && break; sleep 0.1; done
test -s "$root/port"
port=$(cat "$root/port")
mv "$assets/install.sh" "$assets/install.published.sh"
node launcher/scripts/generate-bootstrap.mjs --tag "$tag" --source-revision "$revision" --output "$assets" --asset-base-url "http://127.0.0.1:$port"
archive=
for candidate in "$assets"/speechcraft-*-macos-arm64.tar.gz; do [[ -f "$candidate" ]] && archive=$candidate && break; done
test -n "$archive"
test "$(shasum -a 256 "$archive" | awk '{print $1}')" = "$(awk '{print $1}' "$archive.sha256")"

export HOME="$home" TMPDIR="$root/tmp" PATH="/usr/bin:/bin:/usr/sbin:/sbin"
/bin/sh "$assets/install.sh"
base="$HOME/Library/Application Support/SpeechCraft"
release=$(readlink "$base/app/current")
[[ "$release" == releases/speechcraft-*-macos-arm64 ]]
installed="$base/app/current"
test -x "$installed/runtime/node"
test -x "$installed/bin/speechcraft"
test -f "$installed/frontend/server.js"
test -f "$installed/setup-controller/src/server.js"
test -f "$installed/backend/app/main.py"
file -b "$installed/runtime/node" | grep -qi arm64
[[ "$(lipo -archs "$installed/runtime/node")" == *arm64* ]]
"$installed/runtime/node" -p 'process.arch === "arm64" ? "arm64" : process.arch' | grep -qx arm64

wrapper="$base/bin/speechcraft"
test -x "$wrapper"
"$wrapper" --help >/dev/null
[[ "$(/bin/zsh -l -c 'command -v speechcraft' 2>/dev/null)" == "$wrapper" ]]

diagnose_processes() {
  "$installed/runtime/node" --input-type=module - "$installed" "$base/setup-controller/launcher.processes.json" <<'NODE'
import crypto from 'node:crypto'; import fs from 'node:fs'; import path from 'node:path'; import { pathToFileURL } from 'node:url'; import { execFileSync } from 'node:child_process';
const [root, recordPath] = process.argv.slice(2);
const { ownedProcessIdentity } = await import(pathToFileURL(path.join(root, 'setup-controller/src/bootstrap.js')));
const { records } = JSON.parse(fs.readFileSync(recordPath, 'utf8'));
for (const record of records) {
  const current = ownedProcessIdentity(record.pid, record.nonce);
  const digest = (value) => value ? crypto.createHash('sha256').update(value).digest('hex').slice(0, 12) : null;
  console.log(JSON.stringify({ label: record.label, pid: record.pid, group: record.process_group_id,
    identity_matches: current === record.identity, recorded_identity_sha256_prefix: digest(record.identity),
    current_identity_sha256_prefix: digest(current) }));
  try { process.stdout.write(execFileSync('/bin/ps', ['-o','pid=','-o','pgid=','-o','lstart=','-o','comm=','-p',String(record.pid)], { encoding:'utf8' })); } catch {}
}
NODE
}

"$wrapper" --no-browser >"$root/launch.log" 2>&1 & app_pid=$!
ready=0
for _ in $(seq 1 180); do
  if ! kill -0 "$app_pid" 2>/dev/null; then cat "$root/launch.log" >&2; exit 1; fi
  if curl -fsS http://127.0.0.1:4317/setup-api/health >"$root/controller-health" 2>/dev/null \
      && curl -fsS http://127.0.0.1:3002/setup >"$root/setup.html" 2>/dev/null; then ready=1; break; fi
  sleep 1
done
if [[ $ready != 1 ]]; then cat "$root/launch.log" >&2; find "$HOME/Library/Application Support/SpeechCraft/setup-controller" -type f -print >&2 2>/dev/null || true; exit 1; fi
grep -q '"status":"ok"' "$root/controller-health"
grep -q '<html' "$root/setup.html"
"$wrapper" status --json >"$root/status.json"
"$installed/runtime/node" -e 'const x=require(process.argv[1]); if(!x.running||x.frontend!=="verified") process.exit(1)' "$root/status.json"

diagnose_processes
if ! "$wrapper" stop >"$root/stop.log" 2>&1; then diagnose_processes; exit 1; fi
stopped=0
for _ in $(seq 1 30); do
  if ! curl -fsS http://127.0.0.1:4317/setup-api/health >/dev/null 2>&1 \
      && ! curl -fsS http://127.0.0.1:3002/setup >/dev/null 2>&1; then stopped=1; break; fi
  sleep 1
done
[[ $stopped == 1 ]] || { cat "$root/stop.log" >&2; echo 'owned services remained live after stop' >&2; exit 1; }
wait "$app_pid" || true; app_pid=
"$wrapper" --no-browser >"$root/restart.log" 2>&1 & app_pid=$!
ready=0
for _ in $(seq 1 180); do
  if ! kill -0 "$app_pid" 2>/dev/null; then cat "$root/restart.log" >&2; exit 1; fi
  if curl -fsS http://127.0.0.1:4317/setup-api/health >/dev/null 2>&1 && curl -fsS http://127.0.0.1:3002/setup >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
[[ $ready == 1 ]] || { cat "$root/restart.log" >&2; exit 1; }
mkdir -p "$base/setup-controller/projects" "$base/setup-controller/models" "$base/setup-controller/logs" "$base/setup-controller/exports"
for item in projects models logs exports; do echo keep >"$base/setup-controller/$item/ci-smoke"; done
"$wrapper" stop >/dev/null
"$wrapper" uninstall --yes
test ! -e "$base/app/current"
test ! -e "$wrapper"
for item in projects models logs exports; do test -f "$base/setup-controller/$item/ci-smoke"; done
wait "$app_pid" || true; app_pid=
echo 'macOS ARM64 install, launch, status, restart, stop, and safe uninstall smoke passed.'
