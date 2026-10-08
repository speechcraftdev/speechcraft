#!/usr/bin/env python3
"""Compare representative enrollment and speech edge-case inputs outside the frozen score path."""
import argparse, hashlib, json, sys
from pathlib import Path
import numpy as np
import soundfile as sf
from run import Provider, read_audio, SR

def sha(x): return hashlib.sha256(np.ascontiguousarray(x,dtype=np.float32).tobytes()).hexdigest()
def main():
 p=argparse.ArgumentParser(); p.add_argument('--backend',choices=('nemo','sherpa'),required=True); p.add_argument('--corpus',type=Path,required=True); p.add_argument('--out',type=Path,required=True); p.add_argument('--model',type=Path); a=p.parse_args()
 provider=Provider(a.backend,a.model); clips=[json.loads(x) for x in (a.corpus/'clips.jsonl').read_text().splitlines() if x.strip()]
 chunks=[]
 for source_dir in sorted((a.corpus/'sources').glob('*')):
  mf=source_dir/'enrollment_chunks.jsonl'
  if not mf.exists(): continue
  rows=[json.loads(x) for x in mf.read_text().splitlines() if x.strip()]
  if rows: chunks.append((source_dir.name,rows))
 cases=[]
 # Gold is consulted only to select diagnostic examples; it is not available to the frozen scoring or enrollment path.
 for source,rows in chunks:
  for row in rows:
   if not row.get('gold_wrong_speaker'):
    cases.append((f'clean_target:{source}:{row["chunk_id"]}',Path(row['audio_path'])))
    break
  for row in rows:
   if row.get('gold_wrong_speaker'):
    cases.append((f'known_wrong_speaker:{source}:{row["chunk_id"]}',Path(row['audio_path'])))
    break
 # A deterministic overlap candidate, shortest clip/window, and longest full clip.
 contam=sorted((c for c in clips if float(c.get('non_target_leak_sec') or 0)>0),key=lambda c:(-float(c.get('non_target_leak_sec') or 0),c['source_id'],c['clip_id']))
 if contam: cases.append((f'overlap_clip:{contam[0]["source_id"]}:{contam[0]["clip_id"]}',Path(contam[0]['clip_path'])))
 if clips:
  shortest=min(clips,key=lambda c:(float(c['duration']),c['source_id'],c['clip_id']))
  longest=max(clips,key=lambda c:(float(c['duration']),c['source_id'],c['clip_id']))
  cases.extend([(f'short_clip:{shortest["source_id"]}:{shortest["clip_id"]}',Path(shortest['clip_path'])),(f'long_clip:{longest["source_id"]}:{longest["clip_id"]}',Path(longest['clip_path']))])
 # The product skips mostly-silent windows; these explicit probes check backend vector validity only.
 cases.extend([('synthetic_silence_3s',None),('synthetic_mostly_silence_3s',None)])
 rows=[]; vectors={}
 for item,path in cases:
  if item=='synthetic_silence_3s': samples=np.zeros(SR*3,dtype=np.float32)
  elif item=='synthetic_mostly_silence_3s':
   source=read_audio(Path(chunks[0][1][0]['audio_path'])); samples=np.zeros(SR*3,dtype=np.float32); n=min(len(source),SR//4); samples[:n]=source[:n]
  else: samples=read_audio(path)
  key=item; ih=sha(samples)
  try:
   first=provider.embed(samples); second=provider.embed(samples)
   cosine=float(np.dot(first,second)/(np.linalg.norm(first)*np.linalg.norm(second)))
   vectors[key]=first.astype(np.float32)
   rows.append({'item_id':key,'input_sha256':ih,'input_samples':len(samples),'duration_sec':len(samples)/SR,'embedding_dim':len(first),'finite':bool(np.isfinite(first).all()),'zero_vector':bool(np.linalg.norm(first)<=1e-12),'raw_norm':float(np.linalg.norm(first)),'repeat_cosine':cosine,'repeat_max_abs_diff':float(np.max(np.abs(first-second))),'failure':None})
  except Exception as e: rows.append({'item_id':key,'input_sha256':ih,'input_samples':len(samples),'duration_sec':len(samples)/SR,'embedding_dim':None,'finite':None,'zero_vector':None,'repeat_cosine':None,'repeat_max_abs_diff':None,'failure':f'{type(e).__name__}: {e}'})
 a.out.mkdir(parents=True,exist_ok=True); np.savez_compressed(a.out/'edge_embeddings.npz',**{k.replace(':','__').replace('/','_'):v for k,v in vectors.items()}); (a.out/'edge_embedding_index.json').write_text(json.dumps({'rows':rows,'array_key_map':{k.replace(':','__').replace('/','_'):k for k in vectors},'provider_calls':provider.calls},indent=2)); print(json.dumps({'cases':len(rows),'valid':sum(r['failure'] is None for r in rows)},indent=2))
if __name__=='__main__': main()
