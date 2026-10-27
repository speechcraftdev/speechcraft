#!/usr/bin/env python3
"""Sequential, resumable dispatcher for all frozen generation groups."""
import datetime,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(os.environ.get('SPEECHCRAFT_EXPERIMENT_DIR',Path.cwd())).resolve()
PLAN_PATH=Path(os.environ.get('SPEECHCRAFT_EXPERIMENT_PLAN',str(ROOT/'experiment-plan.json'))).resolve()
if not PLAN_PATH.is_file():raise SystemExit(f'missing experiment plan: {PLAN_PATH}')
PLAN=json.loads(PLAN_PATH.read_text())
VOX=os.environ.get('VOXCPM_PYTHON',sys.executable)
F5=os.environ.get('F5TTS_PYTHON',sys.executable)
SCRIPT=Path(__file__).with_name('generate_condition_group.py')
SPEAKERS=tuple(PLAN.get('speakers',{}))
ARCHS=tuple(a for a in ('voxcpm','f5tts') if a in PLAN.get('models',{}))
MODES=PLAN.get('generation_modes',{'voxcpm':['zero_shot','fine_tuned','reference_free'],'f5tts':['zero_shot','fine_tuned']})
GROUPS=[]
for arch in ARCHS:
 for speaker in SPEAKERS:
  for mode in MODES.get(arch,[]):
   if arch=='f5tts' and mode=='reference_free':continue
   GROUPS.append((arch,speaker,mode))
state_path=ROOT/'generation/queue-state.json';state_path.parent.mkdir(parents=True,exist_ok=True)
if state_path.exists():state=json.loads(state_path.read_text())
else:state={'experiment_id':PLAN.get('experiment_id','local-experiment'),'planned_groups':[{'architecture':a,'speaker':s,'mode':m} for a,s,m in GROUPS],'status':'pending','groups':{},'started_at':None,'updated_at':None}
if state.get('planned_groups')!=[{'architecture':a,'speaker':s,'mode':m} for a,s,m in GROUPS]:raise SystemExit('queue differs from frozen matrix')
def save():
 state['updated_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();tmp=state_path.with_suffix('.json.tmp');tmp.write_text(json.dumps(state,indent=2)+'\n');tmp.replace(state_path)
state['status']='running';state['started_at']=state.get('started_at') or datetime.datetime.now(datetime.timezone.utc).isoformat();save()
for arch,s,mode in GROUPS:
 key=f'{arch}/{s}/{mode}'; rec=state['groups'].get(key,{})
 if rec.get('status')=='complete':continue
 exe=VOX if arch=='voxcpm' else F5;cmd=[exe,str(SCRIPT),'--architecture',arch,'--speaker',s,'--mode',mode,'--resume']
 log=ROOT/'generation'/'queue-logs'/f'{arch}_{s}_{mode}.log';log.parent.mkdir(parents=True,exist_ok=True)
 rec.update({'status':'running','command':cmd,'log':str(log),'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()});state['groups'][key]=rec;save()
 env=os.environ.copy();env['NUMBA_CACHE_DIR']=env.get('NUMBA_CACHE_DIR','/tmp/speechcraft-numba-cache');env['SPEECHCRAFT_EXPERIMENT_DIR']=str(ROOT);env['SPEECHCRAFT_EXPERIMENT_PLAN']=str(PLAN_PATH)
 with log.open('a') as f:
  f.write('\nSTART '+rec['started_at']+'\n');f.flush();proc=subprocess.run(cmd,cwd=ROOT,env=env,stdout=f,stderr=subprocess.STDOUT)
 rec.update({'status':'complete' if proc.returncode==0 else 'error','exit_code':proc.returncode,'completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat()});state['groups'][key]=rec;save()
 if proc.returncode:
  state['status']='paused_error';state['error_group']=key;save();raise SystemExit(f'{key} failed; see {log}; queue is resumable')
 print(f'completed {key} ({len([x for x in state["groups"].values() if x.get("status")=="complete"])}/{len(GROUPS)})',flush=True)
state['status']='complete';state['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save();print(f'all {len(GROUPS)} groups completed')
