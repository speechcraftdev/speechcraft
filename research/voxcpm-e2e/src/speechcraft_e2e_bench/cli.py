from __future__ import annotations
import argparse, json, logging, os, platform, shutil, subprocess, sys
from pathlib import Path
import yaml
from . import __version__
from .evaluate import evaluate_dataset, DEFAULT_ASR_PYTHON
from .manifest import load_dataset, validate_dataset, ManifestError

ROOT=Path(__file__).resolve().parents[2]
def doctor() -> int:
    report={"speechcraft_e2e_bench":__version__,"python":{"version":platform.python_version(),"executable":sys.executable},"system":{"platform":platform.platform(),"ffmpeg":shutil.which("ffmpeg"),"ffprobe":shutil.which("ffprobe")},"evaluators":{}}
    report["evaluators"]["asr_vad"]={"python":os.getenv("SPEECHCRAFT_ASR_PYTHON",DEFAULT_ASR_PYTHON)}
    try:
        from importlib.metadata import version
        report["evaluators"]["jiwer"]={"status":"available","version":version("jiwer")}
    except Exception:
        report["evaluators"]["jiwer"]={"status":"unavailable","reason":"Install the declared jiwer dependency in the benchmark environment."}
    interp=Path(report["evaluators"]["asr_vad"]["python"])
    if interp.is_file():
        proc=subprocess.run([str(interp),"-c","import importlib.metadata as m; print('faster-whisper='+m.version('faster-whisper')); print('silero-vad='+m.version('silero-vad')); import torch; print('torch='+torch.__version__); print('cuda_available='+str(torch.cuda.is_available())); print('cuda_build='+str(torch.version.cuda))"],capture_output=True,text=True)
        report["evaluators"]["asr_vad"]["status"]="available" if proc.returncode==0 else "error"
        report["evaluators"]["asr_vad"]["details"]=(proc.stdout+proc.stderr).strip()
    else: report["evaluators"]["asr_vad"]["status"]="unavailable"
    for key,var,package in (("speaker","SPEECHCRAFT_SPEAKER_PYTHON","speechbrain"),("ttsds2","SPEECHCRAFT_TTSDS_PYTHON","ttsds")):
        path=os.getenv(var)
        report["evaluators"][key]={"python":path,"status":"unavailable"}
        if path and Path(path).is_file():
            p=subprocess.run([path,"-c",f"import importlib.metadata as m; print(m.version('{package}'))"],capture_output=True,text=True)
            report["evaluators"][key].update(status="available" if p.returncode==0 else "error",details=(p.stdout+p.stderr).strip())
    # Read-only inventory gathered before setup; persisted verbatim for provenance.
    report["machine_inventory"]="docs/machine-inventory.md"
    print(json.dumps(report,indent=2)); return 0

def main(argv=None):
    p=argparse.ArgumentParser(prog="speechcraft-e2e",description="Standalone end-to-end TTS evaluation")
    sub=p.add_subparsers(dest="command",required=True)
    sub.add_parser("doctor",help="Inspect evaluator availability")
    v=sub.add_parser("validate",help="Validate manifest and audio"); v.add_argument("dataset")
    e=sub.add_parser("evaluate",help="Run selected metric adapters"); e.add_argument("dataset"); e.add_argument("--metrics",default="secs,consistency,ttsds2,wer,failure"); e.add_argument("--output",default="runs"); e.add_argument("--device",choices=["auto","cpu","cuda"],default="auto"); e.add_argument("--config",help="YAML config for optional evaluator worker paths/settings"); e.add_argument("--min-ttsds-samples",type=int)
    args=p.parse_args(argv)
    if args.command=="doctor": return doctor()
    try:
        ds=load_dataset(args.dataset)
        if args.command=="validate":
            issues=validate_dataset(ds)
            print(json.dumps({"valid":not any(i["severity"]=="error" for i in issues),"speaker_id":ds.speaker_id,"real_recordings":len(ds.real),"prompts":len(ds.prompts),"conditions":{k:len(v) for k,v in ds.conditions.items()},"attempts":len(ds.attempts),"issues":issues},indent=2))
            return 1 if any(i["severity"]=="error" for i in issues) else 0
        config=yaml.safe_load(Path(args.config).read_text()) if args.config else {}
        config=config or {}
        if args.min_ttsds_samples is not None: config.setdefault("ttsds2",{})["minimum_samples"]=args.min_ttsds_samples
        if int(config.get("ttsds2",{}).get("minimum_samples",20))<1: raise ValueError("--min-ttsds-samples must be at least 1")
        selected={"secs":"secs","speaker":"secs","consistency":"consistency","ttsds2":"ttsds2","ttsds":"ttsds2","wer":"wer","failure":"failure","failure_rate":"failure"}
        names={x.strip() for x in args.metrics.split(",") if x.strip()}
        unknown=names-set(selected)
        if unknown: raise ValueError(f"Unknown metrics: {', '.join(sorted(unknown))}. Choose from secs, consistency, ttsds2, wer, failure.")
        metrics={selected[x] for x in names}
        run_dir=evaluate_dataset(ds,metrics=metrics,output=args.output,device=args.device,config=config)
        print(run_dir); return 0
    except (ManifestError,ValueError,OSError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr); return 2

if __name__=="__main__": raise SystemExit(main())
