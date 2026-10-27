#!/usr/bin/env python3
"""Aggregate frozen R0-R10 E2E results; paired prompt bootstrap for SECS only."""
import csv,json,math,random,statistics
from pathlib import Path
ROOT=Path(__import__('os').environ.get('SPEECHCRAFT_SENSITIVITY_RUN_DIR',Path(__file__).resolve().parents[2])).resolve()
_audit_path=ROOT/'reference-audit.json'
AUDIT=json.loads(_audit_path.read_text()) if _audit_path.is_file() else {}
AUDIT_REFS={(x['speaker'],x['reference_id']):x for x in AUDIT.get('references',[])}
SPEAKERS={p.name:p.name for p in ROOT.iterdir() if p.is_dir() and (p/'reference-manifest.json').is_file()}
def load_result(s,r):
 if r=='R0':
  direct=ROOT/s/r/'results.json'
  if direct.is_file():return json.loads(direct.read_text())
  matches=list((ROOT/s/r/'evaluation'/'results').glob('*/results.json'))
 else:matches=list((ROOT/s/r/'evaluation'/'results').glob('*/results.json'))
 if not matches: return None
 return json.loads(sorted(matches,key=lambda p:p.stat().st_mtime)[-1].read_text())
def metric(c,k): return c.get('metrics',{}).get(k,{})
def summary(c,k): return metric(c,k).get('summary') or {}
def extract(res,rid):
 if not res:return None
 c=res.get('conditions',{}).get('zero_shot') if rid=='R0' else res.get('conditions',{}).get(rid)
 if not c:return None
 w=summary(c,'wer'); secs=summary(c,'secs'); cons=metric(c,'consistency').get('generated',{}); t=metric(c,'ttsds2'); tr=t.get('result',{}) if isinstance(t,dict) else {}
 fail=metric(c,'failure_rate').get('result',{}); counts=c.get('failure_counts',metric(c,'failure').get('counts',{}))
 return {'condition':c,'secs':secs.get('mean'),'secs_median':secs.get('median'),'secs_std':secs.get('std'),'secs_p10':secs.get('p10'),'consistency':cons.get('mean'),'consistency_std':cons.get('std'),'consistency_p10':cons.get('p10'),'ttsds_overall':tr.get('overall_score'),'ttsds_categories':tr.get('category_scores',{}),'wer':w.get('wer'),'cer':w.get('cer'),'wer_counts':w,'failure_rate':fail.get('rate'),'failure_denominator':fail.get('denominator'),'failure_attempts':fail.get('failed'),'pass':counts.get('PASS'),'review':counts.get('REVIEW'),'fail':counts.get('FAIL'),'sample_count':metric(c,'secs').get('summary',{}).get('count'),'ttsds_status':t.get('status'),'status':{k:metric(c,k).get('status') for k in ('secs','consistency','ttsds2','wer','failure','failure_rate')}}
def find_clip(res,rid,prompt):
 if not res:return None
 for x in res.get('clips',[]):
  if x.get('prompt_id')==prompt and (x.get('condition')=='zero_shot' if rid=='R0' else x.get('condition')==rid):return x
 return None
def pct(xs,p):
 xs=sorted(xs)
 if not xs:return float('nan')
 z=(len(xs)-1)*p; lo=int(z);hi=min(lo+1,len(xs)-1);return xs[lo]*(hi-z)+xs[hi]*(z-lo)
def paired_bootstrap(diffs,seed):
 if len(diffs)<2:return (float('nan'),float('nan'))
 rng=random.Random(seed);means=[];n=len(diffs)
 for _ in range(10000):means.append(sum(diffs[rng.randrange(n)] for _ in range(n))/n)
 return pct(means,.025),pct(means,.975)
def svg_heatmap(rows,path):
 # rows: list[(label, values)]; outputs a compact dependency-free SVG heatmap.
 allv=[v for _,vals in rows for v in vals if v is not None]
 if not allv:return
 lo=min(allv);hi=max(allv); cell=8; left=95; top=25; width=left+len(rows[0][1])*cell+4; height=top+len(rows)*16+28
 def color(v):
  q=0.5 if hi==lo else (v-lo)/(hi-lo); r=int(245-180*q); g=int(248-85*q); b=int(250-10*q); return f'#{r:02x}{g:02x}{b:02x}'
 out=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">','<rect width="100%" height="100%" fill="white"/>']
 out.append(f'<text x="{left}" y="15" font-family="sans-serif" font-size="11">SECS by evaluation prompt; min={lo:.3f}, max={hi:.3f}</text>')
 for ri,(label,vals) in enumerate(rows):
  y=top+ri*16; out.append(f'<text x="4" y="{y+10}" font-family="sans-serif" font-size="10">{label}</text>')
  for j,v in enumerate(vals):
   if v is not None:out.append(f'<rect x="{left+j*cell}" y="{y}" width="{cell}" height="14" fill="{color(v)}"><title>{label}, prompt {j+1}: {v:.4f}</title></rect>')
 out.append('</svg>');path.write_text('\n'.join(out))

summary_rows=[]; overall=[]
for s,label in SPEAKERS.items():
 reports=[]; prompt_ids=[]; per_ref={}; clips_rows=[]
 results={r:load_result(s,r) for r in ['R0']+[f'R{i}' for i in range(1,11)]}
 extracted={r:extract(results[r],r) for r in results}
 for r,x in extracted.items():
  refnext=(ROOT/s/'reference-manifest.json')
  refs=json.loads(refnext.read_text())['references']
  ref=next((a for a in refs if a['reference_id']==r),{})
  audit_ref=AUDIT_REFS.get((s,r),{})
  duration=ref.get('duration_seconds',audit_ref.get('asr_duration_seconds'))
  if x:
   row={'speaker':label,'reference':r,'duration_s':duration,'source_partition':ref.get('source_partition',audit_ref.get('source_partition')),'secs':x['secs'],'secs_median':x['secs_median'],'secs_std':x['secs_std'],'secs_p10':x['secs_p10'],'consistency':x['consistency'],'consistency_p10':x['consistency_p10'],'ttsds_overall':x['ttsds_overall'],'ttsds_generic':x['ttsds_categories'].get('Generic'),'ttsds_speaker':x['ttsds_categories'].get('Speaker'),'ttsds_prosody':x['ttsds_categories'].get('Prosody'),'ttsds_intelligibility':x['ttsds_categories'].get('Intelligibility'),'wer':x['wer'],'cer':x['cer'],'failure_rate':x['failure_rate'],'failure_denominator':x['failure_denominator'],'pass':x['pass'],'review':x['review'],'review_rate':(x['review']/x['sample_count'] if x['review'] is not None and x['sample_count'] else None),'fail':x['fail'],'primary_attempts':x['failure_denominator'],'valid_generated':x['sample_count'],'ttsds_status':x['ttsds_status'],'metric_statuses':json.dumps(x['status'],sort_keys=True)}
   summary_rows.append(row); reports.append(row)
   per_ref[r]=x
 if results['R0']:
  prompt_ids=[p['id'] for p in results['R0'].get('dataset_manifest',{}).get('prompts',[])]
 # per-prompt outputs and paired SECS bootstrap
 base_scores={}
 for p in prompt_ids:
  clip=find_clip(results['R0'],'R0',p);base_scores[p]=clip.get('speaker_similarity') if clip else None
 for r in ['R0']+[f'R{i}' for i in range(1,11)]:
  vals=[];diffs=[]
  for p in prompt_ids:
   clip=find_clip(results[r],r,p); score=clip.get('speaker_similarity') if clip else None; vals.append(score)
   b=base_scores.get(p)
   lo,hi=(None,None)
   if score is not None and b is not None:diffs.append(score-b)
   if clip:
    clips_rows.append({'speaker':label,'reference':r,'prompt_id':p,'secs':score,'paired_secs_delta_vs_R0':(score-b if score is not None and b is not None else None),'paired_ci95_low':None,'paired_ci95_high':None,'wer':clip.get('wer'),'cer':clip.get('cer'),'recognized_text':clip.get('recognized_text'),'failure_status':clip.get('failure_status'),'failure_reasons':json.dumps(clip.get('failure_reasons',[]),ensure_ascii=False),'path':clip.get('path')})
  if r!='R0' and diffs:
   ci=paired_bootstrap(diffs,20261026+sum(map(ord,s+r)))
   for cr in clips_rows:
    if cr['speaker']==label and cr['reference']==r:cr['paired_ci95_low']=ci[0];cr['paired_ci95_high']=ci[1]
 # write report only if all results present, otherwise progress report with unavailable indicators
 metrics=['secs','consistency','ttsds_overall','ttsds_generic','ttsds_speaker','ttsds_prosody','ttsds_intelligibility','wer','cer','failure_rate']
 lines=[f'# {label}: zero-shot reference sensitivity','',f'Conditions: R0 original plus R1–R10 randomly selected references. Evaluation prompts: {len(prompt_ids)}.','', 'The zero-shot model is the same VoxCPM 1.5 base checkpoint for every condition. R0 is reused from the earlier run only after configuration/provenance matching. TTSDS2 scores are collection-level. Per-prompt bootstrap intervals apply to paired SECS changes only; prompts are the resampling units.','', '| Reference | Duration (s) | SECS | Consistency | TTSDS2 | Generic | Speaker | Prosody | Intelligibility | WER | CER | Failure rate | PASS/REVIEW/FAIL |','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
 for row in reports:
  def fmt(k,n=4):return 'unavailable' if row.get(k) is None else f'{row[k]:.{n}f}'
  lines.append(f"| {row['reference']} | {fmt('duration_s',2)} | {fmt('secs')} | {fmt('consistency')} | {fmt('ttsds_overall',2)} | {fmt('ttsds_generic',2)} | {fmt('ttsds_speaker',2)} | {fmt('ttsds_prosody',2)} | {fmt('ttsds_intelligibility',2)} | {fmt('wer')} | {fmt('cer')} | {fmt('failure_rate')} | {row.get('pass')}/{row.get('review')}/{row.get('fail')} |")
 lines+=['','## Across-reference variation','']
 for m in metrics:
  xs=[float(x[m]) for x in reports if x.get(m) is not None]
  if xs:
   mean=statistics.mean(xs);sd=statistics.stdev(xs) if len(xs)>1 else 0;mn=min(xs);mx=max(xs);base=next((x[m] for x in reports if x['reference']=='R0'),None);rank=1+sum((x[m]>base if m not in ('wer','cer','failure_rate') else x[m]<base) for x in reports if x['reference']!='R0' and x.get(m) is not None) if base is not None else None
   better=sum((x[m]>base if m not in ('wer','cer','failure_rate') else x[m]<base) for x in reports if x['reference']!='R0' and x.get(m) is not None) if base is not None else 0
   lines.append(f'- **{m}:** mean {mean:.4f}, SD {sd:.4f}, range {mn:.4f}–{mx:.4f} (span {mx-mn:.4f}); R0 {base if base is None else f"{base:.4f}"}, rank {rank}/11, new references better than R0: {better}/10.')
  else: lines.append(f'- **{m}:** not yet available for any reference.')
 lines+=['','## Paired prompt SECS changes versus R0','', '| Reference | Mean paired change | 95% prompt-bootstrap CI |','|---|---:|---:|']
 for r in [f'R{i}' for i in range(1,11)]:
  vals=[]
  for p in prompt_ids:
   a=find_clip(results[r],r,p);b=find_clip(results['R0'],'R0',p)
   if a and b and a.get('speaker_similarity') is not None and b.get('speaker_similarity') is not None: vals.append(a['speaker_similarity']-b['speaker_similarity'])
  if vals:
   ci=paired_bootstrap(vals,20261026+sum(map(ord,s+r)))
   lines.append(f'| {r} | {statistics.mean(vals):+.4f} | [{ci[0]:+.4f}, {ci[1]:+.4f}] |')
 lines+=['','## Generation attempt audit','', 'Attempt-level failure rate uses every recorded attempt as its denominator. When an OOM recovery attempt exists, acoustic metrics use the successful recovered WAV; the initial failed attempt remains in this audit and in the failure-rate denominator.','', '| Reference | Primary success/failure | Recovery success/failure | Attempts | Attempt failure rate |','|---|---:|---:|---:|---:|']
 for r in [f'R{i}' for i in range(1,11)]:
  base=ROOT/s/r/'generated'
  if not (base/'attempts-progress.json').exists():
   lines.append(f'| {r} | unavailable | unavailable | 0 | unavailable |'); continue
  primary=json.loads((base/'attempts-progress.json').read_text()).get('attempts',[])
  recovery_path=base/'recovery-attempts.json'; recovery=json.loads(recovery_path.read_text()).get('attempts',[]) if recovery_path.exists() else []
  ps=sum(a.get('status')=='success' for a in primary); pf=len(primary)-ps; rs=sum(a.get('status')=='success' for a in recovery); rf=len(recovery)-rs; den=len(primary)+len(recovery); bad=pf+rf
  lines.append(f'| {r} | {ps}/{pf} | {rs}/{rf} | {den} | {bad/den:.3f} |' if den else f'| {r} | unavailable | unavailable | 0 | unavailable |')
 lines+=['','This report compares reference variants under a fixed zero-shot base model and a fixed prompt/seed schedule. Between-reference differences are descriptive; shared prompts make per-reference outputs dependent. Paired SECS intervals resample prompts, while TTSDS2 remains a collection-level score. Reference-source partitions and transcript audit fields are in `reference-manifest.json` and `reference-audit.json`. No reference was chosen by synthesis quality. Transcript checks were automated ASR screening with the recorded audit fields; they were not independent human listening ratings.','']
 (ROOT/s/'sensitivity-report.md').write_text('\n'.join(lines))
 if prompt_ids:
  heat=[]
  for r in ['R0']+[f'R{i}' for i in range(1,11)]:
   heat.append((r,[((find_clip(results[r],r,p) or {}).get('speaker_similarity')) for p in prompt_ids]))
  svg_heatmap(heat,ROOT/s/'secs-reference-prompt-heatmap.svg')
 with (ROOT/s/'per-prompt-sensitivity.csv').open('w',newline='') as f:
  fields=list(clips_rows[0]) if clips_rows else ['speaker','reference','prompt_id'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(clips_rows)
# global csv
fields=list(summary_rows[0]) if summary_rows else ['speaker','reference']
with (ROOT/'sensitivity-summary.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(summary_rows)
# overall comparison, values are exact metric deltas relative to reference variation, historical FT table separately labeled
lines=['# Zero-shot reference sensitivity v1','', 'The table below summarizes each speaker’s R0–R10 condition range. This experiment measures sensitivity to reference WAV/transcript choice within the base model. It does not estimate a causal comparison with fine-tuning.','', '| Speaker | SECS range | TTSDS2 range | Prosody range | WER range | REVIEW-rate range |','|---|---:|---:|---:|---:|---:|']
for s,label in SPEAKERS.items():
 rr=[r for r in summary_rows if r['speaker']==label]
 def rng(field):
  xs=[r[field] for r in rr if r.get(field) is not None]
  return 'unavailable' if not xs else f'{min(xs):.4f}–{max(xs):.4f} (Δ {max(xs)-min(xs):.4f})'
 lines.append(f"| {label} | {rng('secs')} | {rng('ttsds_overall')} | {rng('ttsds_prosody')} | {rng('wer')} | {rng('review_rate')} |")
lines+=['','## Interpretation note','','Reference-conditioned results are collection-level. SECS prompt bootstrap intervals are paired by prompt; TTSDS2 remains collection-level. Comparisons across references are descriptive unless the model, prompts, evaluator settings, and real collection are held fixed.','']
(ROOT/'overall-report.md').write_text('\n'.join(lines))
print(f'Wrote summaries for {len(summary_rows)} reference results; see {ROOT}/overall-report.md')
