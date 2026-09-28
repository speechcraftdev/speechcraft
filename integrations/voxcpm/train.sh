#!/usr/bin/env bash
# Run the pinned VoxCPM LoRA trainer against a compiled train.jsonl.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROFILE="$HERE/profile.json"
VENDOR_ENV="$HERE/.vendor/env.sh"

if [[ -f "$VENDOR_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$VENDOR_ENV"
fi

MANIFEST=""
OUT=""
STEPS=""
MODEL="${VOXCPM_MODEL:-}"

usage() {
  echo "usage: train.sh --manifest train.jsonl --out /path/to/ckpts [--steps N] [--model /path/to/VoxCPM1.5]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --steps) STEPS="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$MANIFEST" && -n "$OUT" ]] || usage

PYTHON="${VOXCPM_PYTHON:-}"
REPO="${VOXCPM_REPO:-}"
if [[ -z "$PYTHON" || -z "$REPO" ]]; then
  echo "run setup.sh first, or export VOXCPM_PYTHON and VOXCPM_REPO" >&2
  exit 1
fi
if [[ -z "$MODEL" ]]; then
  echo "pass --model or run setup.sh (VOXCPM_MODEL)" >&2
  exit 1
fi

MANIFEST="$(python3 -c 'from pathlib import Path; import sys; p=Path(sys.argv[1]).expanduser().resolve(); assert p.is_file(), p; print(p)' "$MANIFEST")"
MODEL="$(python3 -c 'from pathlib import Path; import sys; p=Path(sys.argv[1]).expanduser().resolve(); assert (p / "config.json").is_file(), p; print(p)' "$MODEL")"
OUT="$(python3 -c 'from pathlib import Path; import sys; p=Path(sys.argv[1]).expanduser().resolve(); p.mkdir(parents=True, exist_ok=True); print(p)' "$OUT")"

CONFIG="$OUT/train.yaml"
"$PYTHON" - "$PROFILE" "$MANIFEST" "$MODEL" "$OUT" "$STEPS" "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

profile_path, manifest, model, out, steps, config_path = sys.argv[1:]
profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
train = dict(profile["train"])
lora = dict(profile["lora"])
if steps:
    n = int(steps)
    train["num_iters"] = n
    train["max_steps"] = n

def dump(value, indent=0):
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, dict):
                lines.append(f"{pad}{key}:")
                lines.append(dump(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {json.dumps(item)}")
        return "\n".join(lines)
    raise TypeError(value)

payload = {
    "pretrained_path": model,
    "train_manifest": manifest,
    "val_manifest": "",
    "sample_rate": int(profile["sample_rate_hz"]),
    **train,
    "save_path": out,
    "tensorboard": str(Path(out) / "tb"),
    "lambdas": {"loss/diff": 1.0, "loss/stop": 1.0},
    "lora": lora,
}
Path(config_path).write_text(dump(payload) + "\n", encoding="utf-8")
print(config_path)
PY

TRAINER="$REPO/scripts/train_voxcpm_finetune.py"
if [[ ! -f "$TRAINER" ]]; then
  echo "missing trainer: $TRAINER" >&2
  exit 1
fi

cd "$REPO"
set +e
"$PYTHON" "$TRAINER" --config_path "$CONFIG"
status=$?
set -e

if [[ "$status" -ne 0 ]]; then
  echo "training failed with status $status" >&2
  exit "$status"
fi

LATEST="$(python3 - <<PY
from pathlib import Path
out = Path("$OUT")
steps = sorted(
    [p for p in out.iterdir() if p.is_dir() and p.name.startswith("step_")],
    key=lambda p: p.name,
)
print(steps[-1] if steps else "")
PY
)"
if [[ -n "$LATEST" ]]; then
  ln -sfn "$(basename "$LATEST")" "$OUT/latest"
  echo "latest -> $LATEST"
fi
echo "train ok: $OUT"
