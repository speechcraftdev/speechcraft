#!/usr/bin/env python3
"""Apply the exact production guillotine/centroid/scorer to saved backend embeddings."""
import argparse, hashlib, json, time
from pathlib import Path
import numpy as np
import soundfile as sf
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'workers/dataset'))
from speechcraft_dataset.eval_speaker_purity import (SpeakerPurityConfig,embedding_vector,outlier_guillotine,iter_sliding_windows,prepare_window_samples,window_is_scorable,score_candidate_clip)
CFG=SpeakerPurityConfig(); SR=16000
def checked_embedding(raw, item_id):
 v=np.asarray(raw,dtype=np.float64).reshape(-1)
 if v.size!=192 or not np.isfinite(v).all() or np.linalg.norm(v)<=1e-12:
  raise ValueError(f'invalid embedding for {item_id}: shape={v.shape}, finite={bool(np.isfinite(v).all())}')
 return embedding_vector(v)
def flagged70(x): return None if x is None else round(float(x)*100.0,2)<70.0
def jl(p): return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
def main():
 p=argparse.ArgumentParser(); p.add_argument('--corpus',type=Path,required=True); p.add_argument('--run',type=Path,required=True); a=p.parse_args()
 idx=json.loads((a.run/'embedding_index.json').read_text()); arrays=np.load(a.run/'embeddings.npz'); reverse={v:k for k,v in idx['array_key_map'].items()}
 clips=sorted(jl(a.corpus/'clips.jsonl'),key=lambda r:(r['source_id'],r['clip_id'])); srcmeta={json.loads(f.read_text())['source_id']:json.loads(f.read_text()) for f in sorted((a.corpus/'sources').glob('*/source.json'))}
 enroll={}; diagnostics=[]; centroids={}; errors={}; score_rows=[]; start=time.perf_counter()
 for source in sorted(srcmeta):
  rows=sorted(jl(a.corpus/'sources'/source/'enrollment_chunks.jsonl'),key=lambda r:r['chunk_id']); em=[]; failed=[]
  for row in rows:
   key=f"enroll:{source}:{row['chunk_id']}"
   if key not in reverse: failed.append(key); continue
   raw=arrays[reverse[key]].astype(np.float64)
   try: v=checked_embedding(raw,key)
   except ValueError: failed.append(key); continue
   em.append((row,v,raw))
  if len(em)!=len(rows):
   errors[source]=f"missing or invalid {len(rows)-len(em)} enrollment embeddings"
   diagnostics.append({'source_id':source,'chunk_count':len(rows),'valid_count':len(em),'kept_count':None,'killed_count':None,'error':errors[source],'failed_embedding_ids':failed})
   continue
  flags,means,cutoff=outlier_guillotine([v for _,v,_ in em],percentile=CFG.outlier_percentile)
  kept=[v for (_,v,_),keep in zip(em,flags) if keep]
  if not kept: errors[source]='empty guillotine output'; continue
  centroid=embedding_vector(np.mean(np.stack(kept),axis=0)); centroids[source]=centroid
  old={r['chunk_id']:r for r in rows}; kept_ids=[]
  details=[]
  for ix,((row,v,raw),flag,mean) in enumerate(zip(em,flags,means)):
   if flag: kept_ids.append(row['chunk_id'])
   details.append({'chunk_id':row['chunk_id'],'kept':bool(flag),'mean_pairwise_similarity':float(mean),'old_kept':old[row['chunk_id']]['guillotine_status']=='kept','gold_wrong_speaker':bool(old[row['chunk_id']].get('gold_wrong_speaker')),'raw_norm':float(np.linalg.norm(raw)),'normalized_norm':float(np.linalg.norm(v))})
  diagnostics.append({'source_id':source,'chunk_count':len(rows),'valid_count':len(em),'kept_count':len(kept_ids),'killed_count':len(rows)-len(kept_ids),'cutoff':cutoff,'kept_chunk_ids':kept_ids,'killed_chunk_ids':[r['chunk_id'] for r,flag,_ in zip(rows,flags,means) if not flag],'old_kept_chunk_ids':[r['chunk_id'] for r in rows if old[r['chunk_id']]['guillotine_status']=='kept'],'wrong_gold_total':sum(d['gold_wrong_speaker'] for d in details),'wrong_gold_kept':sum(d['gold_wrong_speaker'] and d['kept'] for d in details),'chunk_details':details,'centroid_norm':float(np.linalg.norm(centroid)),'centroid_sha256':hashlib.sha256(np.ascontiguousarray(centroid,dtype=np.float64).tobytes()).hexdigest()})
 if centroids: np.savez_compressed(a.run/'centroids.npz',**centroids)
 for clip in clips:
  source,cid=clip['source_id'],clip['clip_id']; row={'source_id':source,'clip_id':cid,'label':not bool(clip['gold_clean']),'leak_sec':float(clip.get('non_target_leak_sec') or 0),'raw_sp_score':None,'sp70_flagged':None,'failure':None}
  try:
   if source not in centroids: raise ValueError(errors.get(source,'source enrollment missing'))
   audio,rate=sf.read(clip['clip_path'],dtype='float32',always_2d=False)
   if rate!=SR or audio.ndim!=1 or not np.isfinite(audio).all(): raise ValueError(f'invalid clip audio rate/shape/values: {rate}/{audio.shape}')
   keys=[]
   for ix,(lo,hi,_t) in enumerate(iter_sliding_windows(audio,rate,window_sec=CFG.window_sec,hop_sec=CFG.window_hop_sec)):
    window=prepare_window_samples(audio[lo:hi],rate,CFG.window_sec)
    if not window_is_scorable(window,rate,CFG)[0]: continue
    key=f'window:{source}/{cid}:{ix:04d}'
    keys.append(key)
   class Cached:
    def __init__(self,vs): self.vs=iter(vs)
    def infer_segment(self,_samples): return next(self.vs),None
   missing=[k for k in keys if k not in reverse]
   if missing: raise ValueError(f'{len(missing)} scoring-window embeddings missing')
   result=score_candidate_clip(Cached([checked_embedding(arrays[reverse[k]],k) for k in keys]),audio,rate,centroids[source],CFG)
   row.update(raw_sp_score=result['purity_score'],sp70_flagged=flagged70(result['purity_score']),scored_windows=result['scored_window_count'])
   if result['purity_score'] is None: row['failure']='no_scorable_windows'
  except Exception as e: row['failure']=f'{type(e).__name__}: {e}'
  score_rows.append(row)
 for enrollment in diagnostics:
  own=[r for r in score_rows if r['source_id']==enrollment['source_id'] and r['raw_sp_score'] is not None]
  clean=[r['raw_sp_score'] for r in own if not r['label']]; dirty=[r['raw_sp_score'] for r in own if r['label']]
  enrollment.update({'clean_scored_count':len(clean),'contaminated_scored_count':len(dirty),'clean_score_median':float(np.median(clean)) if clean else None,'contaminated_score_median':float(np.median(dirty)) if dirty else None,'clean_minus_contaminated_median':float(np.median(clean)-np.median(dirty)) if clean and dirty else None})
 (a.run/'per_clip_scores.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in score_rows))
 (a.run/'enrollment_diagnostics.json').write_text(json.dumps(diagnostics,indent=2))
 payload={'scoring_runtime':'shared production eval_speaker_purity helpers, same Python environment for both backends','scoring_sec':time.perf_counter()-start,'failed_enrollment_sources':errors,'clip_rows':len(score_rows),'scored_clips':sum(r['raw_sp_score'] is not None for r in score_rows),'failure_clips':sum(r['raw_sp_score'] is None for r in score_rows)}
 (a.run/'rescore_metadata.json').write_text(json.dumps(payload,indent=2)); print(json.dumps(payload,indent=2))
if __name__=='__main__': main()
