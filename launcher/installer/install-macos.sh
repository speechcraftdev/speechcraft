#!/bin/sh
set -eu

# macOS installer entrypoint. All downloaded inputs are release-pinned by the
# generated bootstrap. This script uses only macOS base command-line tools.
fail() { printf 'SpeechCraft Mac installer: %s\n' "$*" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || fail 'this installer is for Apple Silicon macOS.'
case "$(uname -m)" in arm64|aarch64) ;; *) fail 'Apple Silicon (ARM64) is required; Intel Mac is unsupported.' ;; esac
[ "$#" -eq 4 ] || fail 'usage: install-macos.sh ARCHIVE EXPECTED_SHA256 EXPECTED_SIZE EXPECTED_REVISION'
archive=$1 expected_sha=$2 expected_size=$3 expected_revision=$4
case "$expected_sha" in *[!0-9a-f]*|'') fail 'invalid archive digest pin.' ;; esac
[ "${#expected_sha}" -eq 64 ] || fail 'invalid archive digest pin.'
case "$expected_size" in *[!0-9]*|'') fail 'invalid archive size pin.' ;; esac
case "$expected_revision" in *[!0-9a-f]*|'') fail 'invalid source revision pin.' ;; esac
[ "${#expected_revision}" -eq 40 ] || fail 'invalid source revision pin.'
command -v shasum >/dev/null 2>&1 || fail 'shasum is required (provided by macOS).'
command -v plutil >/dev/null 2>&1 || fail 'plutil is required (provided by macOS).'
command -v tar >/dev/null 2>&1 || fail 'tar is required (provided by macOS).'
[ -f "$archive" ] && [ ! -L "$archive" ] || fail 'verified archive is not a regular file.'
actual_size=$(stat -f '%z' "$archive") || fail 'could not measure the archive.'
actual_sha=$(shasum -a 256 "$archive" | awk '{print $1}') || fail 'could not hash the archive.'
[ "$actual_size" = "$expected_size" ] && [ "$actual_sha" = "$expected_sha" ] || fail 'archive size or SHA-256 does not match the pinned release.'

home=${HOME:?HOME is unavailable}
case "$home" in /*) ;; *) fail 'HOME must be an absolute path.' ;; esac
base="$home/Library/Application Support/SpeechCraft"
app="$base/app"
releases="$app/releases"
bin="$base/bin"
for parent in "$home/Library" "$home/Library/Application Support" "$base"; do
  if [ -e "$parent" ] || [ -L "$parent" ]; then [ -d "$parent" ] && [ ! -L "$parent" ] || fail "unsafe application directory: $parent";
  else mkdir "$parent" || fail "could not create per-user directory: $parent"; fi
done
mkdir -p "$app" "$releases" "$bin" || fail 'could not create application directories.'
for target in "$app" "$releases" "$bin"; do [ ! -L "$target" ] && [ -d "$target" ] || fail "unsafe installation directory: $target"; done
chmod 700 "$base" "$app" "$releases" "$bin"

list="$releases/.archive-list.$$"
details="$releases/.archive-details.$$"
cleanup() {
  rm -f "$list" "$details"
  if [ -n "${stage:-}" ] && [ -d "$stage" ]; then rm -rf "$stage"; fi
}
trap cleanup EXIT HUP INT TERM
tar -tzf "$archive" > "$list" || fail 'could not read archive directory.'
tar -tvzf "$archive" > "$details" || fail 'could not inspect archive entry types.'
[ -s "$list" ] || fail 'archive is empty.'
if awk 'substr($0,1,1)!="d" && substr($0,1,1)!="-" {bad=1} END {exit bad?0:1}' "$details"; then fail 'archive contains a symlink, hard link, or special file.'; fi
root=
while IFS= read -r member; do
  case "$member" in /*|*../*|../*|*/..|..|*\\*) fail "unsafe archive path: $member" ;; esac
  case "$member" in *[!A-Za-z0-9._/-]*) fail "unsupported archive path: $member" ;; esac
  top=${member%%/*}
  [ -n "$top" ] || fail 'archive has an empty root.'
  if [ -z "$root" ]; then root=$top; else [ "$root" = "$top" ] || fail 'archive must have one top-level application directory.'; fi
done < "$list"
case "$root" in speechcraft-[0-9]*-macos-arm64) ;; *) fail 'archive root does not identify a SpeechCraft macOS ARM64 release.' ;; esac

stage=$(mktemp -d "$releases/.staging.XXXXXXXX") || fail 'could not create private staging.'
chmod 700 "$stage"
# BSD tar rejects traversal, and the explicit member check above rejects it
# before extraction. The archive hash is authenticated by the generated shell.
tar -xzf "$archive" -C "$stage" || fail 'archive extraction failed.'
payload="$stage/$root"
[ -d "$payload" ] && [ ! -L "$payload" ] || fail 'archive application root is missing or unsafe.'
manifest="$payload/application-release-manifest.json"
[ -f "$manifest" ] && [ ! -L "$manifest" ] || fail 'application release manifest is missing or unsafe.'
release_version=${root#speechcraft-}
release_version=${release_version%-macos-arm64}
field() { plutil -extract "$1" raw -o - "$manifest" 2>/dev/null; }
[ "$(field platform)" = macos-arm64 ] || fail 'application manifest platform mismatch.'
[ "$(field source_revision)" = "$expected_revision" ] || fail 'application manifest source revision mismatch.'
[ "$(field version)" = "$release_version" ] || fail 'application manifest version mismatch.'
[ "$(field signing.notarized)" = false ] || fail 'development archive makes an unexpected notarization claim.'
[ -x "$payload/bin/speechcraft" ] && [ -x "$payload/runtime/node" ] || fail 'required executable entrypoints are missing.'

published="$releases/$root"
command="$bin/speechcraft"
if [ -e "$command" ] || [ -L "$command" ]; then
  [ -f "$command" ] && [ ! -L "$command" ] || fail 'speechcraft command conflicts with an unrelated path.'
  cmp -s "$command" - <<'WRAPPER' || fail 'speechcraft command is not an owned SpeechCraft wrapper.'
#!/bin/sh
exec "${HOME}/Library/Application Support/SpeechCraft/app/current/bin/speechcraft" "$@"
WRAPPER
fi
profile="$home/.zprofile"
if [ -L "$profile" ]; then fail '~/.zprofile is a symlink; refusing to modify it.'; fi
if [ -e "$profile" ] && [ ! -f "$profile" ]; then fail '~/.zprofile is not a regular file.'; fi
available_kb=$(df -Pk "$releases" | awk 'NR==2 {print $4}')
required_kb=$(( (expected_size * 2 + 1023) / 1024 ))
case "$available_kb" in *[!0-9]*|'') fail 'could not verify free disk space.' ;; esac
[ "$available_kb" -ge "$required_kb" ] || fail 'insufficient free disk space to stage and verify the application.'
if [ -e "$published" ] || [ -L "$published" ]; then
  [ -d "$published" ] && [ ! -L "$published" ] || fail 'existing release destination is not a real directory.'
  diff -qr "$payload" "$published" >/dev/null || fail 'the same release name exists with different contents; preserving it.'
  rm -rf "$payload"
else
  mv "$payload" "$published" || fail 'could not promote the completed application release.'
fi

current="$app/current"
old_target=
if [ -L "$current" ]; then
  old_target=$(readlink "$current") || fail 'could not inspect active release pointer.'
  case "$old_target" in releases/speechcraft-[0-9]*-macos-arm64) ;; *) fail 'active release pointer is unsafe; preserving the current installation.' ;; esac
elif [ -e "$current" ]; then fail 'active release pointer conflicts with an unrelated file.'; fi
next="$app/.current-next.$$"
rm -f "$next"
ln -s "releases/$root" "$next" || fail 'could not stage active release pointer.'
mv -fh "$next" "$current" || { rm -f "$next"; fail 'could not activate release pointer.'; }

next_command="$bin/.speechcraft-next.$$"
cat > "$next_command" <<'WRAPPER'
#!/bin/sh
exec "${HOME}/Library/Application Support/SpeechCraft/app/current/bin/speechcraft" "$@"
WRAPPER
chmod 755 "$next_command"
if ! mv -f "$next_command" "$command"; then
  if [ -n "$old_target" ]; then ln -s "$old_target" "$next" && mv -fh "$next" "$current"; else rm -f "$current"; fi
  fail 'could not install command wrapper; previous active release restored.'
fi

# Use zprofile for normal zsh login terminals. Refuse symlinks and preserve all
# existing bytes; the marker makes repeat installation idempotent.
marker='# >>> SpeechCraft user command PATH >>>'
if [ -f "$profile" ] && grep -Fq "$marker" "$profile"; then :; else
  profile_next=$(mktemp "$home/.zprofile.speechcraft.XXXXXXXX") || fail 'could not stage zsh PATH configuration.'
  { [ ! -f "$profile" ] || cat "$profile"; printf '\n%s\ncase ":$PATH:" in *:"$HOME/Library/Application Support/SpeechCraft/bin:"*) ;; *) PATH="$HOME/Library/Application Support/SpeechCraft/bin:$PATH" ;; esac\nexport PATH\n# <<< SpeechCraft user command PATH <<<\n' "$marker"; } > "$profile_next" || { rm -f "$profile_next"; fail 'could not stage zsh PATH configuration.'; }
  chmod 600 "$profile_next"
  mv "$profile_next" "$profile" || { rm -f "$profile_next"; fail 'could not update zsh PATH configuration.'; }
fi
rmdir "$stage" || fail 'could not remove completed installation staging directory.'
stage=
rm -f "$list" "$details"
printf 'SpeechCraft %s is installed for this user.\n' "$release_version" >&2
printf 'Open a new terminal to use `speechcraft`, or run: source ~/.zprofile\n' >&2
