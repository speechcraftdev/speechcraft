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
if gh release view "$tag" --repo speechcraftdev/speechcraft >/dev/null 2>&1; then
  printf 'Refusing to overwrite existing release %s.\n' "$tag" >&2
  exit 1
fi

release_work=$(mktemp -d "${TMPDIR:-/tmp}/speechcraft-release.XXXXXXXX")
output="$release_work/assets"
notes="$release_work/notes.md"
cleanup() { rm -rf -- "$release_work"; }
trap cleanup EXIT HUP INT TERM

python3 -m unittest launcher.installer.tests.test_bootstrap launcher.installer.tests.test_install_linux
node launcher/scripts/package-linux.mjs --source-revision "$revision" --output "$output"
archive=$(find "$output" -maxdepth 1 -type f -name 'speechcraft-*-linux-x86_64.tar.gz' -print)
[ -n "$archive" ] && [ "$(printf '%s\n' "$archive" | wc -l | tr -d ' ')" -eq 1 ] || {
  printf 'Expected exactly one Linux x86-64 application archive.\n' >&2
  exit 1
}
node launcher/scripts/generate-bootstrap.mjs --tag "$tag" --source-revision "$revision" --output "$output"
node launcher/scripts/installed-linux-smoke.mjs --archive "$archive" --installer "$output/install-linux.py"
python3 - "$output" "$revision" "$tag" <<'PY'
import hashlib, json, pathlib, sys
output, revision, tag = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
archive = next(output.glob('speechcraft-*-linux-x86_64.tar.gz'))
manifest_path = pathlib.Path(str(archive) + '.release.json')
manifest = json.loads(manifest_path.read_text('utf-8'))
lines = [
    '# SpeechCraft release verification report', '',
    f'- Release identity: `{tag}` (draft; authenticated inspection only)',
    f'- Application version: `{manifest["application_version"]}`',
    f'- Source commit: `{revision}`',
    f'- Platform: `{manifest["platform"]}`',
    f'- Archive: `{archive.name}`',
    f'- Archive SHA-256: `{manifest["archive_sha256"]}`',
    f'- Archive size: `{manifest["archive_size_bytes"]}` bytes',
    f'- Model-manager wheel SHA-256: `{manifest["model_manager_wheel_sha256"]}`',
    '- Validation: bootstrap/installer unit tests and installed-package smoke passed before upload.',
    '- Trust status: internal candidate only. The bootstrap pins exact asset bytes, but this release has no independent publisher signature/attestation verified by the bootstrap.',
    '- Public support: blocked pending FFmpeg redistribution compliance and TitaNet provenance review.',
    '- Package contents: application/runtime files only; no model weights, credentials, local caches, or test datasets.',
    '', '## Uploaded asset SHA-256 values', ''
]
for item in sorted(output.iterdir()):
    if item.is_file() and item.name != 'VERIFICATION.md':
        digest = hashlib.sha256(item.read_bytes()).hexdigest()
        lines.append(f'- `{item.name}` — `{digest}`')
(output / 'VERIFICATION.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
PY
printf 'SpeechCraft %s Linux x86-64 candidate release.\n\nSource commit: `%s`\n\nSee the uploaded `VERIFICATION.md` for archive SHA-256, model-manager wheel identity, asset hashes, and test status.\n\nSupport status: Linux x86-64 package only. macOS Apple Silicon and Windows WSL2 packages are development artifacts and are not production-supported.\n\nThis release is a draft for authenticated inspection. It is not publicly downloadable and does not yet have a bootstrap-verified independent publisher signature/attestation. FFmpeg redistribution review and TitaNet provenance approval remain unresolved; do not publish this release until those gates are documented as cleared. No model weights or developer credentials are included.\n' "$(node -p "require('./frontend/package.json').version")" "$revision" > "$notes"
tag_ref="refs/tags/$tag"
tag_record=$(gh api "repos/speechcraftdev/speechcraft/git/ref/tags/$tag" --jq '.object.type + " " + .object.sha' 2>/dev/null || true)
if [ -n "$tag_record" ]; then
  set -- $tag_record
  if [ "$1" != commit ] || [ "$2" != "$revision" ]; then
    printf 'Refusing to reuse or move existing tag %s.\n' "$tag" >&2
    exit 1
  fi
else
  gh api repos/speechcraftdev/speechcraft/git/refs --method POST -f ref="$tag_ref" -f sha="$revision" >/dev/null
fi
gh release create "$tag" "$output"/* --repo speechcraftdev/speechcraft --target "$revision" --draft \
  --title "SpeechCraft $tag (Linux candidate)" --notes-file "$notes"
