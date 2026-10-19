#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf 'Usage: %s https://github.com/OWNER/REPO/releases/download/TAG/install.sh\n' "$0" >&2
  exit 2
fi
installer_url=$1
case "$installer_url" in https://github.com/speechcraftdev/speechcraft/releases/download/v0.1.1/install.sh) ;; *)
  printf 'Refusing unexpected public installer URL: %s\n' "$installer_url" >&2; exit 2 ;;
esac

work=$(mktemp -d "${TMPDIR:-/tmp}/speechcraft-public-smoke.XXXXXXXX")
export HOME="$work/home"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_STATE_HOME="$HOME/.local/state"
mkdir -p "$HOME" "$work/bin"
browser_log="$work/browser-open.log"
cat > "$work/bin/xdg-open" <<'SH'
#!/bin/sh
printf '%s\n' "$1" >> "$SPEECHCRAFT_BROWSER_OPEN_LOG"
SH
chmod 755 "$work/bin/xdg-open"
export SPEECHCRAFT_BROWSER_OPEN_LOG="$browser_log"
export PATH="$work/bin:$PATH"
launcher_pid=
cleanup() {
  if [ -x "$HOME/.local/bin/speechcraft" ]; then "$HOME/.local/bin/speechcraft" stop >/dev/null 2>&1 || true; fi
  if [ -n "$launcher_pid" ]; then kill "$launcher_pid" >/dev/null 2>&1 || true; wait "$launcher_pid" 2>/dev/null || true; fi
  rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

# Do not pass a GitHub token or curl netrc credentials into this anonymous
# download/install path.
unset GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN
INSTALLER_URL="$installer_url" bash -o pipefail -c \
  'curl --netrc-file /dev/null -fsSL "$INSTALLER_URL" | sh'
test -x "$HOME/.local/bin/speechcraft"
export PATH="$HOME/.local/bin:$PATH"

"$HOME/.local/bin/speechcraft" >"$work/speechcraft.log" 2>&1 &
launcher_pid=$!
ready=false
i=0
while [ "$i" -lt 60 ]; do
  if grep -Fq 'http://127.0.0.1:3002/setup' "$browser_log" 2>/dev/null \
    && curl --netrc-file /dev/null -fsS http://127.0.0.1:3002/setup > "$work/setup.html" 2>/dev/null \
    && grep -q 'Preparing SpeechCraft' "$work/setup.html"; then
    ready=true
    break
  fi
  i=$((i + 1))
  sleep 1
done
[ "$ready" = true ] || { cat "$work/speechcraft.log" >&2; printf 'Frontend did not open and serve the first-run setup page.\n' >&2; exit 1; }
"$HOME/.local/bin/speechcraft" stop
printf 'Public Linux install smoke passed: anonymous release download, isolated per-user install, speechcraft first launch opened /setup, and the setup page rendered over loopback.\n'
