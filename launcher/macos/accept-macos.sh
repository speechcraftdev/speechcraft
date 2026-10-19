#!/bin/sh
set -eu

echo 'SpeechCraft native Mac acceptance preflight; this script never reports acceptance.'
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  echo 'NOT YET VALIDATED: run this on Apple Silicon macOS.' >&2
  exit 2
fi
[ "$#" -eq 1 ] || { echo 'Usage: launcher/macos/accept-macos.sh CANDIDATE_DIRECTORY' >&2; exit 2; }
candidate=$1
[ -d "$candidate" ] && [ ! -L "$candidate" ] || { echo 'Candidate directory must be a real directory.' >&2; exit 2; }
archive=$(find "$candidate" -maxdepth 1 -type f -name 'speechcraft-*-macos-arm64.tar.gz' -print -quit)
[ -n "$archive" ] || { echo 'Mac ARM64 archive is missing.' >&2; exit 2; }
checksum="$archive.sha256"
manifest="$archive.release.json"
[ -f "$checksum" ] && [ ! -L "$checksum" ] && [ -f "$manifest" ] && [ ! -L "$manifest" ] || { echo 'Archive checksum or release manifest is missing/unsafe.' >&2; exit 2; }
(cd "$candidate" && shasum -a 256 -c "$(basename "$checksum")")
echo "Archive: $(basename "$archive")"
echo "SHA-256: $(awk '{print $1}' "$checksum")"
echo "Source revision: $(plutil -extract source_revision raw -o - "$manifest")"
echo "Application version: $(plutil -extract application_version raw -o - "$manifest")"
temp=$(mktemp -d "${TMPDIR:-/tmp}/speechcraft-mac-preflight.XXXXXXXX")
trap 'rm -rf "$temp"' EXIT HUP INT TERM
root=$(tar -tzf "$archive" | sed -n '1s,/.*,,p')
[ -n "$root" ] || { echo 'Could not inspect archive root.' >&2; exit 2; }
tar -xOf "$archive" "$root/runtime/node" > "$temp/node"
chmod 700 "$temp/node"
file "$temp/node"
archs=$(lipo -archs "$temp/node")
[ "$archs" = arm64 ] || { echo "Bundled Node has unexpected architecture: $archs" >&2; exit 2; }
echo 'Pinned bundled Node is a Darwin ARM64 executable; it has not been executed by this preflight.'
echo 'Continue with the clean install, runtime, audio ingestion, full pipeline, upgrade, restart, recovery, and uninstall checklist in launcher/MACOS.md.'
echo 'NOT YET VALIDATED: this preflight does not run the application or its native ML/runtime dependencies.'
exit 2
