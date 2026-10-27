from __future__ import annotations
import json, os, subprocess
from pathlib import Path

def run_worker(python: str, script: str|Path, payload: dict, timeout: int=1800) -> dict:
    p=subprocess.run([python,str(script)],input=json.dumps(payload),text=True,capture_output=True,timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"Evaluator worker failed (exit {p.returncode}): {p.stderr[-4000:]}")
    try: return json.loads(p.stdout)
    except json.JSONDecodeError as e: raise RuntimeError(f"Evaluator worker returned invalid JSON: {p.stdout[-1000:]} {p.stderr[-1000:]}") from e
