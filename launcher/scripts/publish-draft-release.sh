#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  printf 'Usage: %s vX.Y.Z 40-character-source-commit\n' "$0" >&2
  exit 2
fi
tag=$1
revision=$2
case "$tag" in v[0-9]*.[0-9]*.[0-9]*) ;; *) printf 'Release tag must be a new immutable vX.Y.Z tag.\n' >&2; exit 2 ;; esac
case "$revision" in *[!0-9a-f]*|'') printf 'Source revision must be a lowercase 40-character Git commit.\n' >&2; exit 2 ;; esac
[ "${#revision}" -eq 40 ] || { printf 'Source revision must be a lowercase 40-character Git commit.\n' >&2; exit 2; }

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
cd "$repo_root"
head=$(git rev-parse HEAD)
[ "$head" = "$revision" ] || { printf 'Checkout HEAD must equal requested source commit %s.\n' "$revision" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { printf 'Release source checkout must be clean.\n' >&2; exit 1; }
if git ls-remote --exit-code --tags https://github.com/speechcraftdev/speechcraft.git "refs/tags/$tag" "refs/tags/$tag^{}" >/dev/null 2>&1; then
  printf 'Refusing to reuse existing tag %s.\n' "$tag" >&2
  exit 1
fi

release_work=$(mktemp -d "${TMPDIR:-/tmp}/speechcraft-release.XXXXXXXX")
output="$release_work/assets"
notes="$release_work/notes.md"
cleanup() { rm -rf -- "$release_work"; }
trap cleanup EXIT HUP INT TERM

node launcher/scripts/package-linux.mjs --source-revision "$revision" --output "$output"
node launcher/scripts/generate-bootstrap.mjs --tag "$tag" --source-revision "$revision" --output "$output"
printf 'SpeechCraft %s Linux x86-64 candidate release.\n\nSource commit: `%s`\n\nSupport status: Linux x86-64 package only. macOS Apple Silicon and Windows WSL2 packages are development artifacts and are not production-supported.\n\nThis release is a draft for authenticated inspection. It is not publicly downloadable. FFmpeg redistribution review and TitaNet provenance approval remain unresolved; do not publish this release until both gates are documented as cleared. No model weights or developer credentials are included.\n' "$(node -p "require('./frontend/package.json').version")" "$revision" > "$notes"
gh release create "$tag" "$output"/* --repo speechcraftdev/speechcraft --target "$revision" --draft \
  --title "SpeechCraft $tag (Linux candidate)" --notes-file "$notes"
