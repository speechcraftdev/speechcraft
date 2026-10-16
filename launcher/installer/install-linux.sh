#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ "$#" -ne 1 ]; then printf 'Usage: %s speechcraft-<version>-linux-x86_64.tar.gz\n' "$0" >&2; exit 2; fi
exec python3 "$script_dir/install-linux.py" "$1"
