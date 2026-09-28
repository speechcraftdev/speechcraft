#!/usr/bin/env bash
# Pin, clone, venv, and download VoxCPM1.5 weights. Does not train.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROFILE="$HERE/profile.json"
VENDOR="$HERE/.vendor"
REPO="$VENDOR/VoxCPM"
VENV="$VENDOR/venv"
MODEL="$VENDOR/models/openbmb__VoxCPM1.5"
ENV_FILE="$VENDOR/env.sh"

if [[ ! -f "$PROFILE" ]]; then
  echo "missing $PROFILE" >&2
  exit 1
fi

COMMIT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["voxcpm_commit"])' "$PROFILE")"
GIT_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["voxcpm_git"])' "$PROFILE")"
HF_MODEL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["hf_model_id"])' "$PROFILE")"

if [[ -z "$COMMIT" || -z "$GIT_URL" ]]; then
  echo "profile.json is missing voxcpm_commit / voxcpm_git" >&2
  exit 1
fi

mkdir -p "$VENDOR" "$VENDOR/models"

if [[ -d "$REPO/.git" ]]; then
  CURRENT="$(git -C "$REPO" rev-parse HEAD)"
  if [[ "$CURRENT" != "$COMMIT" ]]; then
    echo "existing clone is $CURRENT, fetching pin $COMMIT"
    git -C "$REPO" fetch --depth 1 origin "$COMMIT"
    git -C "$REPO" checkout --detach "$COMMIT"
  fi
else
  rm -rf "$REPO"
  git init --quiet "$REPO"
  git -C "$REPO" remote add origin "$GIT_URL"
  git -C "$REPO" fetch --depth 1 origin "$COMMIT"
  git -C "$REPO" checkout --quiet --detach FETCH_HEAD
fi

PINNED="$(git -C "$REPO" rev-parse HEAD)"
if [[ "$PINNED" != "$COMMIT" ]]; then
  echo "checkout landed on $PINNED, expected $COMMIT" >&2
  exit 1
fi
echo "VoxCPM source pinned at $PINNED"

if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
"$VENV/bin/python" -m pip install -e "$REPO"
"$VENV/bin/python" -m pip install huggingface_hub

"$VENV/bin/python" - <<PY
from pathlib import Path
from huggingface_hub import snapshot_download

dest = Path("$MODEL")
dest.mkdir(parents=True, exist_ok=True)
path = snapshot_download(repo_id="$HF_MODEL", local_dir=str(dest))
print(path)
PY

if [[ ! -f "$MODEL/config.json" ]]; then
  echo "model download did not produce $MODEL/config.json" >&2
  exit 1
fi

cat > "$ENV_FILE" <<EOF
export VOXCPM_REPO="$REPO"
export VOXCPM_MODEL="$MODEL"
export VOXCPM_PYTHON="$VENV/bin/python"
export VOXCPM_COMMIT="$COMMIT"
EOF

echo "wrote $ENV_FILE"
echo "setup ok"
