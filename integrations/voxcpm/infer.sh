#!/usr/bin/env bash
# Synthesize one utterance from a LoRA checkpoint.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENDOR_ENV="$HERE/.vendor/env.sh"
if [[ -f "$VENDOR_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$VENDOR_ENV"
fi

if [[ -z "${VOXCPM_PYTHON:-}" ]]; then
  echo "run setup.sh first, or export VOXCPM_PYTHON" >&2
  exit 1
fi

exec "$VOXCPM_PYTHON" "$HERE/infer.py" "$@"
