#!/usr/bin/env bash
# Resume JRE dataset run from MFA through slicer + QC (default params).
set -euo pipefail

RUN_ROOT="/home/aaravthegreat/Projects/speechcraft/backend/data/media/dataset-runs/jre-mqq8v7lz/dataset-2305bd7a0d28"
WORKER_ROOT="/home/aaravthegreat/Projects/speechcraft/workers/dataset"
PYTHON="$WORKER_ROOT/.venv/bin/python"
LOG="$RUN_ROOT/logs/jre_resume.log"

mkdir -p "$HOME/Documents/MFA" "$RUN_ROOT/logs"
export PATH="$HOME/.conda/envs/speechcraft-mfa/bin:$PATH"
export SPEECHCRAFT_MFA_BIN="$HOME/.conda/envs/speechcraft-mfa/bin/mfa"
export SPEECHCRAFT_MFA_DICTIONARY=english_us_mfa
export SPEECHCRAFT_MFA_ACOUSTIC_MODEL=english_mfa

exec >>"$LOG" 2>&1
echo "[$(date -Iseconds)] === JRE resume: MFA → alignment_qc → slicer → QC ==="

cd "$WORKER_ROOT"
PYTHONPATH=. "$PYTHON" - <<'PY'
from pathlib import Path
from speechcraft_dataset.io import read_json
from speechcraft_dataset.mfa import run_mfa_alignment
from speechcraft_dataset.alignment_qc import run_alignment_qc
from speechcraft_dataset.run import log_line, write_status, utc_now_iso

run_root = Path("/home/aaravthegreat/Projects/speechcraft/backend/data/media/dataset-runs/jre-mqq8v7lz/dataset-2305bd7a0d28")
config = read_json(run_root / "config.json")

write_status(run_root, {"ok": None, "stage": "mfa", "started_at": utc_now_iso()})
mfa = run_mfa_alignment(run_root, config)
log_line(run_root, f"mfa completed summary={mfa}")

write_status(run_root, {"ok": None, "stage": "alignment_qc", "summary": mfa})
aqc = run_alignment_qc(run_root, config)
log_line(run_root, f"alignment_qc completed summary={aqc}")
write_status(run_root, {"ok": True, "stage": "alignment_qc", "summary": aqc, "completed_at": utc_now_iso()})
PY

echo "[$(date -Iseconds)] === slicer rerun (default params) ==="
PYTHONPATH=. "$PYTHON" -m speechcraft_dataset.rerun_slicer \
  --run-root "$RUN_ROOT" \
  --config "$RUN_ROOT/config.json"

echo "[$(date -Iseconds)] === QC score generation ==="
PYTHONPATH=. "$PYTHON" -m speechcraft_dataset.generate_qc_scores \
  --run-root "$RUN_ROOT" \
  --config "$RUN_ROOT/config.json" \
  --force

echo "[$(date -Iseconds)] === JRE resume done ==="
