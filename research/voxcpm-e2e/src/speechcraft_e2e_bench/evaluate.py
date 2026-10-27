from __future__ import annotations
import csv, json, logging, os, platform, sys, time, uuid, subprocess
import importlib.util
import hashlib
from pathlib import Path
from typing import Any
import numpy as np
import yaml
import soundfile as sf
from . import __version__
from .aggregates import describe, pairwise_cosine, speaker_similarity
from .audio import load_audio, cache_key
from .failures import classify, failure_rate, apply_override
from .manifest import Dataset
from .textmetrics import score_pair, aggregate as aggregate_text
from .worker import run_worker

LOG=logging.getLogger("speechcraft_e2e_bench")
BASE=Path(__file__).resolve().parents[2]
DEFAULT_ASR_PYTHON=sys.executable

def _resolve_device(python: str, requested: str) -> str:
    """Use CUDA only when the selected worker runtime reports it available."""
    if requested == "cpu": return "cpu"
    proc=None
    try:
        proc=subprocess.run([python,"-c","import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"],capture_output=True,text=True,timeout=60)
        available=proc.returncode==0 and proc.stdout.strip().endswith("cuda")
    except Exception:
        available=False
    if requested == "cuda" and not available:
        detail=((proc.stderr or proc.stdout).strip()[-1000:] if proc else "worker could not inspect CUDA")
        raise RuntimeError(f"CUDA was requested but is unavailable in {python}: {detail}")
    return "cuda" if available else "cpu"

def _versions() -> dict:
    versions={"speechcraft-e2e-bench":__version__,"python":platform.python_version()}
    for package in ("numpy","scipy","soundfile","PyYAML","jiwer","rapidfuzz"):
        try:
            from importlib.metadata import version
            versions[package]=version(package)
        except Exception: pass
    return versions

def _cache_read(path: Path, key: str):
    try: return json.loads((path/(key+".json")).read_text())
    except Exception: return None

def _cache_write(path: Path, key: str, value: Any):
    path.mkdir(parents=True,exist_ok=True)
    tmp=path/(key+".tmp"); tmp.write_text(json.dumps(value)); tmp.replace(path/(key+".json"))

def _write_outputs(run_dir: Path, result: dict, clips: list[dict], config: dict):
    (run_dir/"results.json").write_text(json.dumps(result,indent=2,ensure_ascii=False,default=str)+"\n")
    (run_dir/"run_config.yaml").write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
    (run_dir/"dataset_manifest.yaml").write_text(yaml.safe_dump(result["dataset_manifest"],sort_keys=False,allow_unicode=True))
    (run_dir/"provenance.json").write_text(json.dumps({"dependencies":result["environment"].get("packages",{}),"evaluators":result["provenance"]},indent=2,ensure_ascii=False,default=str)+"\n")
    (run_dir/"dependencies.txt").write_text("\n".join(f"{k}=={v}" for k,v in sorted(result["environment"].get("packages",{}).items()))+"\n")
    fields=["condition","clip_id","prompt_id","path","duration_seconds","transcript","recognized_text","wer","cer","wer_status","wer_error","speaker_similarity","generation_attempt_id","seed","outcome","model","checkpoint","reference_clip","speaker_error","asr_error","vad_error","failure_status","failure_reasons"]
    with (run_dir/"clips.csv").open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore"); w.writeheader()
        for row in clips:
            meta=row.get("metadata",{})
            row={**row,**{k:meta.get(k) for k in ("generation_attempt_id","seed","outcome","model","checkpoint","reference_clip")},"failure_reasons":"; ".join(row.get("failure_reasons",[]))}; w.writerow(row)
    lines=["# Speechcraft E2E Benchmark Report","",f"Run: `{result['run_id']}`",f"Dataset: `{result['dataset']}`",f"Speaker: `{result['speaker_id']}`","",
           "Higher is better: speaker cosine similarity, speaker consistency, and TTSDS2 scores. Lower is better: WER, CER, and generation failure rate.","",
           "## Dataset and evaluator status","",f"Real recordings: {result['counts']['real']} | Prompts: {result['counts']['prompts']} | Attempt manifest entries: {result['counts']['attempts']} | Complete: {result['counts']['attempts_complete']}",""]
    for issue in result.get("validation",[]): lines.append(f"- {issue['severity'].upper()}: {issue['message']}")
    baseline=result.get("real_asr_baseline",{"status":"not_requested"})
    if baseline.get("status")=="available": lines.append(f"- Natural-speech ASR reference baseline: WER {baseline['summary']['wer']}; CER {baseline['summary']['cer']}; samples={baseline['summary']['sample_count']}.")
    elif baseline.get("status")=="unavailable": lines.append(f"- Natural-speech ASR reference baseline: unavailable — {baseline.get('reason')}.")
    lines += ["","## Conditions",""]
    for name,c in result["conditions"].items():
        lines += [f"### {name}","",f"Samples: {c['counts']['supplied']} supplied; {c['counts']['valid']} valid; {c['counts']['excluded']} excluded.",""]
        if c.get("metadata"): lines.append("Condition model metadata: "+json.dumps(c["metadata"],ensure_ascii=False,default=str)+".")
        excluded=[r for r in clips if r.get("condition")==name and not r.get("readable",True)]
        if excluded: lines.append("Excluded clips: "+"; ".join(f"{r.get('clip_id')}: {r.get('audio_error')}" for r in excluded)+".")
        for key,label in (("secs","Speaker similarity (higher is better)"),("consistency","Speaker consistency (higher is better)"),("wer","WER (lower is better)"),("failure_rate","Generation failure rate (lower is better)"),("ttsds2","TTSDS2 collection score (higher is better)")):
            val=c["metrics"].get(key,{"status":"not_requested"})
            if val.get("status")=="available":
                if key=="secs": lines.append(f"- {label}: mean {val['summary']['mean']:.4f}, median {val['summary']['median']:.4f}, SD {val['summary']['std']:.4f}, p10 {val['summary']['p10']:.4f} (n={val['summary']['count']}).")
                elif key=="consistency": lines.append(f"- {label}: mean {val['generated']['mean'] if val['generated']['mean'] is not None else 'n/a'}; p10 {val['generated']['p10'] if val['generated']['p10'] is not None else 'n/a'}; pairs={val['generated']['pair_count']}.")
                elif key=="wer": lines.append(f"- {label}: WER {val['summary']['wer']}; CER {val['summary']['cer']}; samples={val['summary']['sample_count']}.")
                elif key=="failure_rate": lines.append(f"- {label}: {val['result']['rate']} ({val['result']['failed']}/{val['result']['denominator']}; {val['result']['label']}).")
                else:
                    t=val.get("result",{}); categories=t.get("category_scores",{})
                    if t.get("overall_score") is not None:
                        lines.append(f"- {label}: overall {t['overall_score']}; Speaker {categories.get('Speaker')}; Prosody {categories.get('Prosody')}; Intelligibility {categories.get('Intelligibility')}; Generic {categories.get('Generic')}.")
                    else: lines.append(f"- {label}: upstream result fields are present, but no overall score was returned; see `results.json`.")
            else: lines.append(f"- {label}: {val.get('status','unavailable')} — {val.get('reason','not requested')}.")
        if c.get("failure_counts"): lines.append(f"- Failure classifications: {json.dumps(c['failure_counts'])}.")
        failure_reasons=sorted({reason for r in clips if r.get("condition")==name for reason in r.get("failure_reasons",[])})
        if failure_reasons: lines.append("- Classification reasons: "+"; ".join(failure_reasons)+".")
        lines.append("")
    lines += ["## Provenance","",f"Python: {result['environment']['python']} | OS: {result['environment']['platform']}",f"ASR: {result['provenance'].get('asr',{})}",f"Speaker encoder: {result['provenance'].get('speaker',{})}",f"TTSDS: {result['provenance'].get('ttsds',{})}","","Full per-clip details and errors are in `results.json` and `clips.csv`."]
    (run_dir/"report.md").write_text("\n".join(lines)+"\n")

def evaluate_dataset(ds: Dataset, *, metrics: set[str]|None=None, output: str|Path="runs", device: str="auto", config: dict|None=None, thresholds: dict|None=None) -> Path:
    all_metrics={"secs","consistency","ttsds2","wer","failure"}
    requested=metrics if metrics is not None else all_metrics
    invalid=requested-all_metrics
    if invalid: raise ValueError(f"Unknown metrics: {', '.join(sorted(invalid))}")
    defaults={"metrics":sorted(requested),"audio":{"sample_rate":16000,"channels":1,"resampling":"scipy.signal.resample_poly; arithmetic mono downmix"},"speaker":{"backend":"speechbrain","model":"speechbrain/spkrec-ecapa-voxceleb","license":"Apache-2.0","enrollment":"L2-normalize mean of valid real recording embeddings; score each synthesized embedding against centroid"},"asr":{"backend":"faster-whisper","model":"large-v3","version":"1.2.1","checkpoint":{"model_id":"Systran/faster-whisper-large-v3","revision":"edaa852ec7e145841d8ffdb056a99866b5f0a478","license":"MIT"},"python":os.getenv("SPEECHCRAFT_ASR_PYTHON",DEFAULT_ASR_PYTHON),"device":"cpu","compute_type":"int8","beam_size":5,"normalization":"Unicode NFKC, casefold, punctuation replaced with spaces, whitespace collapsed"},"vad":{"backend":"silero-vad","version":"6.2.1","license":"MIT"},"ttsds2":{"package":"ttsds","version":"2.1.3","license":"MIT","weights":{"Speaker":.25,"Prosody":.25,"Intelligibility":.25,"Generic":.25,"Environment":0.0},"include_environment":False,"minimum_samples":20},"failure_thresholds":thresholds or {}}
    cfg={**defaults,**(config or {})}
    for section in ("audio","speaker","asr","vad","ttsds2","failure_thresholds"):
        cfg[section]={**defaults[section],**((config or {}).get(section) or {})}
    if cfg["speaker"].get("backend")=="ecapa2":
        if "model" not in ((config or {}).get("speaker") or {}): cfg["speaker"]["model"]="Jenthe/ECAPA2"
        cfg["speaker"]["license"]="CC-BY-NC-4.0"
    effective_thresholds=thresholds if thresholds is not None else cfg.get("failure_thresholds",{})
    if int(cfg["ttsds2"].get("minimum_samples",20))<1: raise ValueError("TTSDS2 minimum_samples must be at least 1")
    base=Path(output).resolve(); base.mkdir(parents=True,exist_ok=True)
    run_id=time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:8]; run_dir=base/run_id; run_dir.mkdir()
    cache=base.parent/"cache"; cache.mkdir(parents=True,exist_ok=True)
    issues=[]
    from .manifest import validate_dataset
    issues=validate_dataset(ds)
    real_items=[x for x in ds.real]
    generated_items=[(name,x) for name,items in ds.conditions.items() for x in items]
    clips=[]; evaluation_audio={}
    eval_audio_dir=run_dir/"audio"; eval_audio_dir.mkdir()
    for item in real_items:
        try:
            audio,sr=load_audio(item.path)
            eval_path=eval_audio_dir/(hashlib.sha256((item.kind+item.id).encode()).hexdigest()[:16]+".wav")
            sf.write(eval_path,audio,sr,subtype="FLOAT"); evaluation_audio[item.id]=str(eval_path)
        except Exception as exc:
            issues.append({"severity":"error","message":f"{item.id}: cannot create evaluation copy ({exc})"})
    for cond,item in generated_items:
        try:
            audio,sr=load_audio(item.path); duration=len(audio)/sr
            eval_path=eval_audio_dir/(hashlib.sha256((item.kind+item.id).encode()).hexdigest()[:16]+".wav")
            sf.write(eval_path,audio,sr,subtype="FLOAT"); evaluation_audio[item.id]=str(eval_path)
            readable=True; err=None
        except Exception as exc: duration=None; readable=False; err=str(exc)
        clips.append({"condition":cond,"clip_id":item.id,"prompt_id":item.prompt_id,"path":str(item.path),"duration_seconds":duration,"readable":readable,"audio_error":err,"transcript":ds.prompts.get(item.prompt_id),"wer_status":"not_requested" if "wer" not in requested else "pending","metadata":item.metadata})
    results={}; provenance={"asr":{},"speaker":{},"ttsds":{}}
    for name,items in ds.conditions.items():
        rows=[r for r in clips if r["condition"]==name]
        results[name]={"metadata":ds.condition_metadata.get(name,{}),"counts":{"supplied":len(items),"valid":sum(r["readable"] for r in rows),"excluded":sum(not r["readable"] for r in rows)},"metrics":{},"failure_counts":{}}
    # Transcription and VAD share a single model-loaded process where installed.
    asr_python=cfg["asr"].get("python") or os.getenv("SPEECHCRAFT_ASR_PYTHON") or DEFAULT_ASR_PYTHON
    voice_payload={"model":cfg["asr"].get("model","large-v3"),"device":_resolve_device(asr_python,device),"compute_type":cfg["asr"].get("compute_type","int8"),"beam_size":int(cfg["asr"].get("beam_size",5)),"language":cfg["asr"].get("language"),"transcribe":[],"vad":[]}
    cfg["asr"]["device"]=voice_payload["device"]
    cached_transcripts={}; cached_vad={}; transcribe_items={}; vad_items={}
    for row in clips:
        item=next(x for c,x in generated_items if x.id==row["clip_id"])
        if "wer" in requested and importlib.util.find_spec("jiwer") and row["readable"]:
            key=cache_key(item.path,"faster-whisper",cfg["asr"].get("version","unknown"),{"model":voice_payload["model"],"language":voice_payload["language"],"beam_size":voice_payload["beam_size"],"compute_type":voice_payload["compute_type"],"audio_transform":"mono-f32-16khz-resample_poly-v1"})
            val=_cache_read(cache,key)
            if val is not None: cached_transcripts[item.id]=val
            else: voice_payload["transcribe"].append({"id":item.id,"path":evaluation_audio[item.id]}); transcribe_items[item.id]=(item,key)
        if "failure" in requested and row["readable"]:
            key=cache_key(item.path,"silero-vad",cfg["vad"].get("version","unknown"),{"sample_rate":16000,"audio_transform":"mono-f32-16khz-resample_poly-v1"})
            val=_cache_read(cache,key)
            if val is not None: cached_vad[item.id]=val
            else: voice_payload["vad"].append({"id":item.id,"path":evaluation_audio[item.id]}); vad_items[item.id]=(item,key)
    if "wer" in requested and cfg.get("transcribe_real",False) and importlib.util.find_spec("jiwer"):
        for item in real_items:
            if not item.transcript or item.id not in evaluation_audio: continue
            key=cache_key(item.path,"faster-whisper",cfg["asr"].get("version","unknown"),{"model":voice_payload["model"],"language":voice_payload["language"],"beam_size":voice_payload["beam_size"],"compute_type":voice_payload["compute_type"],"audio_transform":"mono-f32-16khz-resample_poly-v1"})
            cached=_cache_read(cache,key)
            if cached is not None: cached_transcripts[item.id]=cached
            else: voice_payload["transcribe"].append({"id":item.id,"path":evaluation_audio[item.id]}); transcribe_items[item.id]=(item,key)
    voice_result={}
    if "wer" in requested or "failure" in requested:
        provenance["asr"]={"python":cfg["asr"].get("python") or DEFAULT_ASR_PYTHON,"model":voice_payload["model"],"checkpoint":cfg["asr"].get("checkpoint"),"version":cfg["asr"].get("version"),"status":"pending" if voice_payload["transcribe"] else "not_used"}
        provenance["vad"]={"backend":cfg["vad"].get("backend"),"version":cfg["vad"].get("version"),"license":cfg["vad"].get("license"),"status":"pending" if voice_payload["vad"] else "not_used"}
    if voice_payload["transcribe"] or voice_payload["vad"]:
        try:
            if not Path(asr_python).is_file(): raise FileNotFoundError(f"ASR/VAD interpreter not found: {asr_python}")
            voice_result=run_worker(asr_python,BASE/"workers/asr_vad_worker.py",voice_payload)
            for ident,(item,key) in transcribe_items.items():
                if ident in voice_result.get("transcripts",{}): _cache_write(cache,key,voice_result["transcripts"][ident])
            for ident,(item,key) in vad_items.items():
                if ident in voice_result.get("vad",{}): _cache_write(cache,key,voice_result["vad"][ident])
            provenance["asr"]={**provenance["asr"],"python":asr_python,"version":voice_result.get("versions",{}).get("faster-whisper",cfg["asr"].get("version")),"torch":voice_result.get("versions",{}).get("torch"),"torchaudio":voice_result.get("versions",{}).get("torchaudio"),"cuda_available":voice_result.get("versions",{}).get("cuda_available"),"cuda_build":voice_result.get("versions",{}).get("cuda_build"),"device":voice_payload["device"],"status":"used"}
            provenance["vad"]={"version":voice_result.get("versions",{}).get("silero-vad",cfg["vad"].get("version")),"backend":"Silero VAD","license":cfg["vad"].get("license"),"status":"used" if voice_payload["vad"] else "not_used"}
        except Exception as exc:
            voice_result={"asr_error":str(exc) if voice_payload["transcribe"] else None,"vad_error":str(exc) if voice_payload["vad"] else None}
            if voice_payload["transcribe"]: provenance["asr"].update(status="error",error=str(exc))
            if voice_payload["vad"]: provenance["vad"].update(status="error",error=str(exc))
    if cached_transcripts and not voice_payload["transcribe"]: provenance["asr"]["status"]="cache_hit"
    if cached_vad and not voice_payload["vad"]: provenance["vad"]["status"]="cache_hit"
    voice_result.setdefault("transcripts",{}).update(cached_transcripts)
    voice_result.setdefault("vad",{}).update(cached_vad)
    transcript_by_id=voice_result.get("transcripts",{}); vad_by_id=voice_result.get("vad",{})
    if "wer" in requested:
        scores={name:[] for name in ds.conditions}
        jiwer_missing=not importlib.util.find_spec("jiwer")
        if not jiwer_missing:
            for row in clips:
                rec=transcript_by_id.get(row["clip_id"],{})
                if rec.get("text") is not None and row["transcript"] is not None:
                    sc=score_pair(row["transcript"],rec["text"]); row.update(sc); row["wer_status"]="available"; scores[row["condition"]].append(sc)
                else:
                    row["wer_status"]="unavailable"
                    row["wer_error"]="Intended transcript or successful ASR transcription is missing."
                    if rec.get("error"): row["asr_error"]=rec["error"]
        for name in ds.conditions:
            if jiwer_missing:
                stat={"status":"unavailable","reason":"JiWER is not installed in the benchmark environment; install the declared jiwer dependency to calculate WER/CER."}
                for row in clips:
                    if row["condition"]==name: row["wer_status"]="unavailable"; row["wer_error"]=stat["reason"]
            elif voice_result.get("asr_error"): stat={"status":"unavailable","reason":voice_result["asr_error"]}
            else:
                agg=aggregate_text(scores[name]); stat={"status":"available" if agg["sample_count"] else "unavailable","summary":agg,"reason":None if agg["sample_count"] else "No clip had both intended text and a successful ASR transcription."}
            results[name]["metrics"]["wer"]=stat
        for row in clips:
            if not row.get("recognized_text"):
                rec=transcript_by_id.get(row["clip_id"],{})
                if rec.get("error") or voice_result.get("asr_error"): row["asr_error"]=rec.get("error") or voice_result.get("asr_error")
    real_asr_baseline={"status":"not_requested"}
    if "wer" in requested and cfg.get("transcribe_real",False):
        if not importlib.util.find_spec("jiwer"):
            real_asr_baseline={"status":"unavailable","reason":"JiWER is not installed in the benchmark environment."}
        else:
            real_scores=[]
            for item in real_items:
                rec=transcript_by_id.get(item.id,{})
                if item.transcript and rec.get("text") is not None: real_scores.append(score_pair(item.transcript,rec["text"]))
            summary=aggregate_text(real_scores)
            real_asr_baseline={"status":"available" if summary["sample_count"] else "unavailable","summary":summary,"reason":None if summary["sample_count"] else "No real recording had both a supplied transcript and a successful ASR transcription."}
    vectors={}
    speaker_py=cfg.get("speaker",{}).get("python") or os.getenv("SPEECHCRAFT_SPEAKER_PYTHON")
    speaker_backend=cfg.get("speaker",{}).get("backend","speechbrain")
    speaker_model=cfg.get("speaker",{}).get("model","speechbrain/spkrec-ecapa-voxceleb")
    speaker_revision=cfg.get("speaker",{}).get("revision","main")
    if requested & {"secs","consistency"}:
        items=[x for x in [*real_items,*[x for _,x in generated_items if next(r for r in clips if r["clip_id"]==x.id)["readable"]]] if x.id in evaluation_audio]
        if speaker_backend not in {"speechbrain","ecapa2"}:
            reason=f"Unsupported speaker backend {speaker_backend!r}; supported backends are speechbrain and ecapa2."
            for name in ds.conditions:
                for metric in requested & {"secs","consistency"}: results[name]["metrics"][metric]={"status":"unavailable","reason":reason}
            provenance["speaker"]={"backend":speaker_backend,"model":speaker_model,"status":"unavailable","reason":reason}
        elif speaker_py and Path(speaker_py).is_file() and items:
            speaker_settings={"device":_resolve_device(speaker_py,device),"audio_transform":"mono-f32-16khz-resample_poly-v1"}
            try:
                model_cache=str(cfg["speaker"].get("cache_dir") or cache/"speaker-models")
                resolved=run_worker(speaker_py,BASE/"workers/speaker_worker.py",{"action":"resolve","backend":speaker_backend,"model":speaker_model,"revision":speaker_revision,"model_cache":model_cache,"local_files_only":bool(cfg["speaker"].get("local_files_only",False))})
                if resolved.get("error"): raise RuntimeError(resolved["error"])
                resolved_revision=resolved.get("resolved_revision",speaker_revision)
                speaker_settings["revision"]=resolved_revision
                misses=[]
                for x in items:
                    key=cache_key(x.path,"speaker-"+speaker_backend,resolved_revision,{**speaker_settings,"model":speaker_model})
                    val=_cache_read(cache,key)
                    if isinstance(val,list): vectors[x.id]=val
                    else: misses.append((x,key))
                payload={"backend":speaker_backend,"model":speaker_model,"revision":resolved_revision,"device":speaker_settings["device"],"model_cache":model_cache,"local_files_only":True,"items":[{"id":x.id,"path":evaluation_audio[x.id]} for x,_ in misses]}
                embres=run_worker(speaker_py,BASE/"workers/speaker_worker.py",payload) if misses else {"embeddings":{},"versions":{"cache":"hit"},"resolved_revision":resolved_revision}
                if embres.get("error"): raise RuntimeError(embres["error"])
                license_id="Apache-2.0" if speaker_backend=="speechbrain" else "CC-BY-NC-4.0"
                provenance["speaker"]={"python":speaker_py,"backend":speaker_backend,"checkpoint":{"model_id":speaker_model,"revision":resolved_revision},"license":license_id,"versions":embres.get("versions",{})}
                embedding_errors={}
                for x,key in misses:
                    val=embres.get("embeddings",{}).get(x.id)
                    if isinstance(val,list): vectors[x.id]=val; _cache_write(cache,key,val)
                    elif isinstance(val,dict) and val.get("error"): embedding_errors[x.id]=val["error"]
                for row in clips:
                    if row["clip_id"] in embedding_errors: row["speaker_error"]=embedding_errors[row["clip_id"]]
                for name in ds.conditions:
                    gen={x.id:vectors[x.id] for x in ds.conditions[name] if x.id in vectors}
                    real={x.id:vectors[x.id] for x in real_items if x.id in vectors}
                    if "secs" in requested:
                        sim=speaker_similarity(real,gen); summ=describe(list(sim.values()))
                        results[name]["metrics"]["secs"]={"status":"available" if sim else "unavailable","summary":summ,"per_clip":sim,"reason":None if sim else "Valid reference and generated speaker embeddings are required."}
                        for row in clips:
                            if row["condition"]==name and row["clip_id"] in sim: row["speaker_similarity"]=sim[row["clip_id"]]
                    if "consistency" in requested:
                        results[name]["metrics"]["consistency"]={"status":"available" if len(gen)>=2 else "unavailable","generated":pairwise_cosine(gen),"real_baseline":pairwise_cosine(real),"reason":None if len(gen)>=2 else "At least two valid generated clips in this condition are required."}
            except Exception as exc:
                for name in ds.conditions:
                    for metric in requested & {"secs","consistency"}: results[name]["metrics"][metric]={"status":"error","reason":str(exc)}
        else:
            reason=f"Speaker worker for {speaker_backend} is not configured. Set SPEECHCRAFT_SPEAKER_PYTHON to an isolated Python with its dependencies installed." if not speaker_py else "Configured speaker worker interpreter does not exist."
            license_id="Apache-2.0" if speaker_backend=="speechbrain" else "CC-BY-NC-4.0"
            provenance["speaker"]={"backend":speaker_backend,"model":speaker_model,"license":license_id,"status":"unavailable","reason":reason}
            for name in ds.conditions:
                for metric in requested & {"secs","consistency"}: results[name]["metrics"][metric]={"status":"unavailable","reason":reason}
    # Explicit failure statuses for supplied clips and known attempts.
    if "failure" in requested:
        for row in clips:
            vad=vad_by_id.get(row["clip_id"]); vad=vad if isinstance(vad,dict) and "error" not in vad else None
            vad_result=vad_by_id.get(row["clip_id"])
            if isinstance(vad_result,dict) and vad_result.get("error"): row["vad_error"]=vad_result["error"]
            cls=classify(artifact_exists=True,readable=row["readable"],outcome=row["metadata"].get("outcome"),vad=vad,duration=row["duration_seconds"],wer=row.get("wer"),cer=row.get("cer"),transcript=row.get("recognized_text"),reference_text=row.get("transcript"),thresholds=effective_thresholds,vad_unavailable=vad is None)
            review=ds.raw.get("review_overrides",{}).get(row["clip_id"])
            cls=apply_override(cls,review); row["failure_status"]=cls["status"]; row["failure_reasons"]=cls["reasons"]; row["review_override"]=cls["review_override"]
        clip_ids={x.id for _,x in generated_items}
        matched={x.metadata.get("generation_attempt_id") for _,x in generated_items}
        matched.update(x.metadata.get("attempt_id") for _,x in generated_items)
        matched.update(clip_ids)
        matched.update(a["id"] for a in ds.attempts if a.get("clip_id") in clip_ids)
        missing=[a for a in ds.attempts if a["id"] not in matched and not (a.get("output_path") and Path(a["output_path"]).is_file())]
        denominator_attempts=[{**a,"output_path":a.get("output_path"),"clip_id":a.get("clip_id")} for a in ds.attempts]
        for name in ds.conditions:
            attempts=[]
            condition_clip_ids={x.id for x in ds.conditions[name]}
            for a in denominator_attempts:
                if a.get("condition")!=name: continue
                artifact_found=bool(a.get("output_path") and Path(str(a["output_path"])).is_file()) or a.get("clip_id") in condition_clip_ids or a["id"] in matched
                attempts.append({**a,"artifact_missing":not artifact_found})
            observable_cond=sum(r.get("condition")==name and r.get("failure_status")=="FAIL" for r in clips)
            supplied=len([r for r in clips if r.get("condition")==name])
            fr=failure_rate(attempts,observable_cond,supplied,complete=bool(ds.raw.get("attempts_complete",False)))
            for a in missing:
                if a.get("condition")==name:
                    itemrow={"condition":name,"clip_id":None,"prompt_id":a.get("prompt_id"),"path":None,"duration_seconds":None,"failure_status":"FAIL","failure_reasons":["Attempted generation has no output artifact."],"attempt_id":a["id"]}
                    clips.append(itemrow); observable_cond+=1
            if attempts: fr=failure_rate(attempts,observable_cond,len(attempts),complete=bool(ds.raw.get("attempts_complete",False)))
            results[name]["metrics"]["failure_rate"]={"status":"available" if fr["rate"] is not None else "unavailable","result":fr}
            results[name]["failure_counts"]={s:sum(r.get("condition")==name and r.get("failure_status")==s for r in clips) for s in ("PASS","REVIEW","FAIL")}
            results[name]["metrics"]["failure"]={"status":"available" if supplied or attempts else "unavailable","counts":results[name]["failure_counts"],"per_clip":{r.get("clip_id") or r.get("attempt_id"): {"status":r.get("failure_status"),"reasons":r.get("failure_reasons",[]),"review_override":r.get("review_override")} for r in clips if r.get("condition")==name}}
    if "ttsds2" in requested:
        py=cfg.get("ttsds2",{}).get("python") or os.getenv("SPEECHCRAFT_TTSDS_PYTHON")
        for name,items in ds.conditions.items():
            valid=[x for x in items if next(r for r in clips if r.get("clip_id")==x.id)["readable"]]
            min_samples=int(cfg["ttsds2"].get("minimum_samples",20))
            if len(valid)<min_samples:
                results[name]["metrics"]["ttsds2"]={"status":"unavailable","reason":f"TTSDS2 distribution score requires at least {min_samples} valid generated recordings under configured policy; got {len(valid)}. A tiny API smoke test is not an interpretable score."}
                continue
            if not py or not Path(py).is_file():
                results[name]["metrics"]["ttsds2"]={"status":"unavailable","reason":"TTSDS worker is not configured. Set SPEECHCRAFT_TTSDS_PYTHON to the isolated TTSDS environment interpreter."}
                continue
            try:
                root=run_dir/"staging"; refdir=root/"reference"; slug=hashlib.sha256(name.encode()).hexdigest()[:12]; gendir=root/f"condition-{slug}"; refdir.mkdir(parents=True,exist_ok=True); gendir.mkdir(parents=True,exist_ok=True)
                for idx,x in enumerate(ds.real):
                    a,sr=load_audio(evaluation_audio[x.id]); sf.write(refdir/f"{idx:05d}.wav",a,sr,subtype="PCM_16")
                for idx,x in enumerate(valid):
                    a,sr=load_audio(evaluation_audio[x.id]); sf.write(gendir/f"{idx:05d}.wav",a,sr,subtype="PCM_16")
                ttsds_device=_resolve_device(py,device)
                ttsds_cache=str(cfg["ttsds2"].get("cache_dir") or cache/"ttsds")
                out=run_worker(py,BASE/"workers/ttsds_worker.py",{"generated_dir":str(gendir),"reference_dir":str(refdir),"condition":name,"individual_csv":str(run_dir/f"ttsds-{slug}-individual.csv"),"cache_dir":ttsds_cache,"device":ttsds_device})
                provenance["ttsds"]={"python":py,"version":out.get("version"),"package":"ttsds","license":"MIT","environment_included":False,"weights":out.get("weights")}
                if out.get("error"): results[name]["metrics"]["ttsds2"]={"status":"error","reason":out["error"],"diagnostic":out}
                elif not out.get("complete",False):
                    results[name]["metrics"]["ttsds2"]={"status":"partial","reason":"Official TTSDS results include one or more failed/non-finite benchmark results or lack one of the four primary categories; no complete overall score is reported.","sample_count":len(valid),"reference_sample_count":len(ds.real),"result":out}
                else: results[name]["metrics"]["ttsds2"]={"status":"available","sample_count":len(valid),"reference_sample_count":len(ds.real),"result":out}
            except Exception as exc: results[name]["metrics"]["ttsds2"]={"status":"error","reason":str(exc)}
    if "failure" not in requested:
        for name,items in ds.conditions.items():
            results[name]["metrics"]["failure_rate"]={"status":"not_requested"}
            results[name]["metrics"]["failure"]={"status":"not_requested"}
    for name in ds.conditions:
        for m in all_metrics:
            if m not in results[name]["metrics"]: results[name]["metrics"][m]={"status":"not_requested"}
    result={"schema_version":1,"run_id":run_id,"created_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"dataset":str(ds.path),"dataset_manifest":ds.raw,"speaker_id":ds.speaker_id,"real_asr_baseline":real_asr_baseline,
            "counts":{"real":len(ds.real),"prompts":len(ds.prompts),"attempts":len(ds.attempts),"attempts_complete":bool(ds.raw.get("attempts_complete",False)),"generated_supplied":len(generated_items)},"validation":issues,
            "conditions":results,"clips":clips,"provenance":provenance,"environment":{"python":platform.python_version(),"platform":platform.platform(),"packages":_versions()},
            "errors":{"asr":voice_result.get("asr_error"),"vad":voice_result.get("vad_error")}}
    config_out={**cfg,"run_id":run_id,"dataset":str(ds.path),"environment":result["environment"],"provenance":provenance}
    _write_outputs(run_dir,result,clips,config_out)
    log=logging.getLogger("speechcraft_e2e_bench.run")
    handler=logging.FileHandler(run_dir/"run.log"); handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s")); log.addHandler(handler)
    for issue in issues:
        getattr(log,"error" if issue["severity"]=="error" else "warning")(issue["message"])
    for condition,summary in results.items():
        for metric,metric_result in summary["metrics"].items():
            if metric_result.get("status") in {"error","unavailable"}:
                log.warning("condition=%s metric=%s status=%s reason=%s",condition,metric,metric_result.get("status"),metric_result.get("reason"))
    log.info("Completed run %s with conditions=%s",run_id,list(ds.conditions)); log.removeHandler(handler); handler.close()
    return run_dir
