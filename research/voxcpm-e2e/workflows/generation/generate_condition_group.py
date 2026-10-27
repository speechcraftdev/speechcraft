#!/usr/bin/env python3
"""Restartable generation for one architecture/speaker/mode group.

A group loads one model once and processes its 10 matched references (or the
VoxCPM reference-free condition). Each attempt is journaled atomically before
and after inference. Existing terminal attempts are never silently replaced.
"""
import argparse,contextlib,fcntl,gc,hashlib,io,json,os,random,subprocess,sys,time,traceback
from pathlib import Path
import numpy as np
import soundfile as sf

ROOT=Path(os.environ.get('SPEECHCRAFT_EXPERIMENT_DIR',Path(__file__).resolve().parents[2])).resolve()
PLAN_PATH=Path(os.environ.get('SPEECHCRAFT_EXPERIMENT_PLAN',str(ROOT/'experiment-plan.json'))).resolve()
BASE_VOX=Path(os.environ.get('VOXCPM_BASE_MODEL',''))
VOX_REPO=Path(os.environ.get('VOXCPM_REPO',''))
F5_REPO=Path(os.environ.get('F5TTS_REPO',''))
F5_BASE=Path(os.environ.get('F5TTS_BASE_CHECKPOINT',str(F5_REPO/'ckpts/F5TTS_v1_Base/model_1250000.safetensors')))

def sha(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def sha_text(value):return hashlib.sha256(value.encode('utf-8')).hexdigest() if isinstance(value,str) else None
def atomic_json(path,obj):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n');tmp.replace(path)
def jsonl(path):return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]
def frozen_lora(s,plan):
 spec=plan['speakers'][s].get('voxcpm',{})
 configured=spec.get('checkpoint')
 if configured:
  path=Path(configured);return path if path.is_absolute() else ROOT/path
 meta=json.loads((ROOT/'training/voxcpm'/s/'selected-checkpoint.json').read_text());return Path(meta['checkpoint']).parent

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--architecture',choices=['voxcpm','f5tts'],required=True);ap.add_argument('--speaker',required=True);ap.add_argument('--mode',choices=['zero_shot','fine_tuned','reference_free'],required=True);ap.add_argument('--resume',action='store_true');ap.add_argument('--retry-known-progress-bug',action='store_true',help='Archive and retry only F5 attempts that failed with the known progress callback AttributeError.');a=ap.parse_args()
 arch,s,mode=a.architecture,a.speaker,a.mode
 if arch=='f5tts' and mode=='reference_free':raise SystemExit('reference-free inference is VoxCPM-only')
 lockfile=(ROOT/'gpu.lock').open('w')
 try:fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise SystemExit(f'GPU lock already held; refusing parallel job. See {ROOT}/run-state.json')
 plan=json.loads(PLAN_PATH.read_text())
 if s not in plan.get('speakers',{}):raise SystemExit(f'unknown speaker id: {s}')
 if arch=='voxcpm':
  if not BASE_VOX.is_dir() or not VOX_REPO.is_dir():raise SystemExit('set VOXCPM_BASE_MODEL and VOXCPM_REPO to local paths')
  if not __import__('torch').cuda.is_available():raise RuntimeError('CUDA unavailable; refusing CPU substitution')
  voxcpm_plan=plan['models']['voxcpm']
  actual_commit=subprocess.check_output(['git','-C',str(VOX_REPO),'rev-parse','HEAD'],text=True).strip()
  if voxcpm_plan.get('source_commit') and actual_commit!=voxcpm_plan['source_commit']:raise RuntimeError('VoxCPM source commit differs from frozen plan')
  base_meta={'path':str(BASE_VOX),'revision':voxcpm_plan.get('base_revision'),'config_sha256':sha(BASE_VOX/'config.json'),'weights_sha256':sha(BASE_VOX/'model.safetensors'),'source_commit':actual_commit}
  if voxcpm_plan.get('base_config_sha256') and base_meta['config_sha256']!=voxcpm_plan['base_config_sha256']:raise RuntimeError('VoxCPM base config differs from frozen plan')
  if voxcpm_plan.get('base_weights_sha256') and base_meta['weights_sha256']!=voxcpm_plan['base_weights_sha256']:raise RuntimeError('VoxCPM base checkpoint differs from frozen plan')
 else:
  if not __import__('torch').cuda.is_available():raise RuntimeError('CUDA unavailable; refusing CPU substitution')
  f5_plan=plan['models']['f5tts']
  actual_commit=subprocess.check_output(['git','-C',str(F5_REPO),'rev-parse','HEAD'],text=True).strip()
  if f5_plan.get('source_commit') and actual_commit!=f5_plan['source_commit']:raise RuntimeError('F5-TTS source commit differs from frozen plan')
  base_meta={'path':str(F5_BASE),'revision':f5_plan.get('base_revision'),'checkpoint_sha256':sha(F5_BASE),'source_commit':actual_commit,'vocoder':f5_plan.get('vocoder'),'vocoder_revision':f5_plan.get('vocoder_revision')}
  if f5_plan.get('pretrained_checkpoint_sha256') and base_meta['checkpoint_sha256']!=f5_plan['pretrained_checkpoint_sha256']:raise RuntimeError('F5 base checkpoint differs from frozen plan')
 pmeta=plan['prompts']; prompt_file=Path(pmeta.get('manifest','shared_synthetic_prompts.jsonl')); prompt_file=prompt_file if prompt_file.is_absolute() else ROOT/prompt_file
 prompts=jsonl(prompt_file)
 if pmeta.get('sha256') and sha(prompt_file)!=pmeta['sha256']:raise RuntimeError('shared prompt manifest hash mismatch')
 refs_file=Path(plan.get('references',{}).get('manifest','references-final.jsonl')); refs_file=refs_file if refs_file.is_absolute() else ROOT/refs_file
 allrefs=[x for x in jsonl(refs_file) if x['speaker']==s]
 if len(allrefs)!=10:raise RuntimeError(f'{s}: expected 10 frozen references, found {len(allrefs)}')
 refs=([{'speaker':s,'reference_id':'NO_REFERENCE','audio':None,'transcript':None,'audio_sha256':None}] if mode=='reference_free' else allrefs)
 if mode=='reference_free' and arch!='voxcpm':raise SystemExit('reference-free inference is VoxCPM-only')
 outbase=ROOT/'generation'/arch/s/mode;outbase.mkdir(parents=True,exist_ok=True)
 adapter=None
 if mode in {'fine_tuned','reference_free'}:
  if arch=='voxcpm':adapter=frozen_lora(s,plan)
  else:
   selected=json.loads((ROOT/'training/f5tts'/s/'selected-checkpoint.json').read_text());adapter=Path(selected['selected_checkpoint'])
 if adapter is not None:
  target=adapter/'lora_weights.safetensors' if arch=='voxcpm' and adapter.is_dir() else adapter
  if not target.is_file():raise FileNotFoundError(f'missing selected adapter/checkpoint: {target}')
  if arch=='voxcpm':
   expected=plan['speakers'][s].get('voxcpm',{}).get('checkpoint_sha256')
   if expected and sha(target)!=expected:raise RuntimeError(f'{s} LoRA SHA256 does not match frozen checkpoint provenance')
 adapter_meta={'path':str(adapter),'weights_path':str(target),'sha256':sha(target)} if adapter else None
 settings=plan['inference'][arch]
 if arch=='f5tts':settings={**settings,'target_rms':settings.get('target_rms',0.1),'cross_fade_duration':settings.get('cross_fade_duration',0.15),'output_format':settings.get('output_format','WAV PCM_16'),'use_ema':settings.get('use_ema',True)}
 seed_base=int(settings.get('seed_base',202610280000))
 settings={**settings,'seed_base':seed_base,'seed_schedule':settings.get('seed_schedule','seed_base + zero-based prompt index; paired conditions reset Python/NumPy/Torch RNG per prompt')}
 group_meta={'architecture':arch,'speaker':s,'mode':mode,'base':base_meta,'adapter':adapter_meta,'prompt_manifest_sha256':pmeta['sha256'],'settings':settings,'reference_provenance':{r['reference_id']:{'path':r['audio'],'audio_sha256':r['audio_sha256'],'transcript_sha256':sha_text(r['transcript']),'historical_source_partition':r.get('historical_source_partition'),'exposure_by_frozen_split_hash':r.get('exposure_by_frozen_split_hash'),'transcript_audit_status':r.get('transcript_audit_status'),'known_transcript_warning':r.get('known_transcript_warning')} for r in refs}}
 if arch=='voxcpm':
  import torch
  random.seed(seed_base);np.random.seed(seed_base%(2**32));torch.manual_seed(seed_base);torch.cuda.manual_seed_all(seed_base)
  sys.path.insert(0,str(VOX_REPO/'src'));from voxcpm.core import VoxCPM
  model=VoxCPM.from_pretrained(hf_model_id=str(BASE_VOX),load_denoiser=False,optimize=False,lora_weights_path=str(adapter) if adapter else None)
 else:
  import torch
  random.seed(seed_base);np.random.seed(seed_base%(2**32));torch.manual_seed(seed_base);torch.cuda.manual_seed_all(seed_base)
  sys.path.insert(0,str(F5_REPO/'src'));from f5_tts.api import F5TTS
  vocab=ROOT/'training/f5tts/preprocessed'/f'{s}_train/vocab.txt'
  model=F5TTS(model='F5TTS_v1_Base',ckpt_file=str(adapter or F5_BASE),vocab_file=str(vocab),use_ema=settings.get('use_ema',True),device='cuda')
 print(f'loaded {arch} {mode} {s} on {__import__("torch").cuda.get_device_name(0)}',flush=True)
 for ref in refs:
  rid=ref['reference_id']; refpath=Path(ref['audio']) if ref['audio'] else None
  if refpath is not None and (not refpath.is_file() or sha(refpath)!=ref['audio_sha256']):raise RuntimeError(f'{s}/{rid}: reference file/hash mismatch')
  reftext=ref['transcript']
  dest=outbase/rid;dest.mkdir(parents=True,exist_ok=True);journal=dest/'attempts.json'
  if journal.exists():
   state=json.loads(journal.read_text())
   if state.get('group_meta')!=group_meta or state.get('reference_sha256')!=ref['audio_sha256']:raise RuntimeError(f'{s}/{mode}/{rid}: frozen metadata mismatch')
   if a.retry_known_progress_bug and arch=='f5tts' and state.get('attempts'):
    statuses={x.get('status') for x in state['attempts']}
    known_bug=all((x.get('status')=='error' and x.get('error_type')=='AttributeError' and "'function' object has no attribute 'tqdm'" in x.get('error','')) or (x.get('status')=='started' and not Path(x.get('output_path','')).is_file() and not state.get('attempts_complete')) for x in state['attempts'])
    if known_bug:
     stamp=time.strftime('%Y%m%dT%H%M%S',time.gmtime())
     archive=dest/f'attempts-prior-progress-callback-{stamp}.json'
     if any(dest.glob('*.wav')):raise RuntimeError(f'{s}/{mode}/{rid}: refusing retry because output WAVs already exist')
     archive.write_text(journal.read_text())
     state={'group_meta':group_meta,'reference_id':rid,'reference_path':str(refpath) if refpath else None,'reference_sha256':ref['audio_sha256'],'reference_transcript':reftext,'attempts':[],'attempts_complete':False,'rerun_reason':'Fixed F5 infer(progress=...) callback; prior all-error journal archived.','prior_attempt_journal':str(archive)}
     print(f'{s}/{mode}/{rid}: archived known callback failures and retrying all prompts',flush=True)
    elif 'error' in statuses or 'started' in statuses or 'interrupted_unknown' in statuses:
     raise RuntimeError(f'{s}/{mode}/{rid}: retry flag found non-matching or mixed attempt history; refusing to overwrite it')
   if state.get('attempts_complete') and not state.get('rerun_reason'):
    print(f'{s}/{mode}/{rid}: already complete',flush=True);continue
   for row in state['attempts']:
    if row['status']=='started':row.update(status='interrupted_unknown',error='interrupted after attempt started; not rerun automatically')
   done={x['prompt_id'] for x in state['attempts']}
  else:
   if not a.resume and any(dest.glob('*.wav')):raise RuntimeError(f'outputs exist without journal in {dest}; refusing overwrite')
   state={'group_meta':group_meta,'reference_id':rid,'reference_path':str(refpath) if refpath else None,'reference_sha256':ref['audio_sha256'],'reference_transcript':reftext,'attempts':[],'attempts_complete':False};done=set()
  atomic_json(journal,state)
  for i,prompt in enumerate(prompts):
   if prompt['id'] in done:continue
   seed=seed_base+i;target=dest/f"{prompt['id']}.wav"
   row={'prompt_id':prompt['id'],'target_text':prompt['text'],'seed':seed,'output_path':str(target),'status':'started','started_at':time.time(),'reference_id':rid}
   state['attempts'].append(row);atomic_json(journal,state);start=time.time()
   try:
    import torch
    random.seed(seed);np.random.seed(seed%(2**32));torch.manual_seed(seed);torch.cuda.manual_seed_all(seed)
    if arch=='voxcpm':
     wav=model.generate(text=prompt['text'],prompt_wav_path=str(refpath) if refpath else None,prompt_text=reftext,cfg_value=settings.get('cfg_value',2.0),inference_timesteps=settings.get('inference_timesteps',10),min_len=settings.get('min_len',2),max_len=settings.get('max_len',4096),normalize=settings.get('normalize',False),denoise=settings.get('denoise',False),retry_badcase=settings.get('retry_badcase',False))
     arr=np.asarray(wav,dtype=np.float32).reshape(-1);sr=44100
    else:
     wav,sr,_=model.infer(ref_file=str(refpath),ref_text=reftext,gen_text=prompt['text'],seed=seed,target_rms=settings.get('target_rms',0.1),cross_fade_duration=settings.get('cross_fade_duration',0.15),sway_sampling_coef=settings.get('sway_sampling_coef',-1),cfg_strength=settings.get('cfg_strength',2),nfe_step=settings.get('nfe_step',32),speed=settings.get('speed',1.0),fix_duration=settings.get('fix_duration'),remove_silence=settings.get('remove_silence',False),show_info=lambda *_:None,progress=None)
     arr=np.asarray(wav,dtype=np.float32).reshape(-1)
    if arr.size==0 or not np.isfinite(arr).all():raise ValueError('generated audio is empty or non-finite')
    sf.write(str(target),arr,sr,subtype='PCM_16')
    row.update(status='success',sample_rate=sr,duration_seconds=arr.size/sr,sha256=sha(target),elapsed_seconds=time.time()-start,completed_at=time.time())
   except Exception as e:
    row.update(status='error',error_type=type(e).__name__,error=str(e),traceback=traceback.format_exc(),elapsed_seconds=time.time()-start,completed_at=time.time())
   atomic_json(journal,state)
   print(f'{arch} {s} {mode}/{rid} {i+1}/50 {row["status"]} {prompt["id"]}',flush=True)
  state['attempts_complete']=True;state['completed_at']=time.time();atomic_json(journal,state)
  print(f'{arch} {s} {mode}/{rid}: {sum(x["status"]=="success" for x in state["attempts"])}/50 complete',flush=True)
 del model;gc.collect()
 try:
  import torch;torch.cuda.empty_cache()
 except Exception:pass
if __name__=='__main__':main()
