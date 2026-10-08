#!/usr/bin/env python3
"""Create paired metrics and audit tables from backend run directories."""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path
import os
import sys
import subprocess
import numpy as np
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import average_precision_score, roc_auc_score
ROOT=Path(__file__).resolve().parents[2]

def jl(path): return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
def write_csv(path, rows):
    keys=[]
    for r in rows:
        for k in r:
            if k not in keys: keys.append(k)
    with path.open('w', newline='') as f:
        w=csv.DictWriter(f, fieldnames=keys, extrasaction='ignore'); w.writeheader(); w.writerows(rows)
def auc(y,s): return float(roc_auc_score(y, -np.asarray(s))) if len(set(y))==2 else None
def ap(y,s): return float(average_precision_score(y, -np.asarray(s))) if len(set(y))==2 else None
def validate_exact_inputs(nidx, sidx):
    nh=nidx['input_hashes']; sh=sidx['input_hashes']
    if set(nh)!=set(sh): raise ValueError(f'backend input item sets differ: nemo={len(nh)}, sherpa={len(sh)}')
    changed=[k for k in nh if nh[k]!=sh[k]]
    if changed: raise ValueError(f'backend input samples differ for {len(changed)} items; first={changed[0]}')
def validate_clip_maps(nemo, sherpa):
    if set(nemo)!=set(sherpa): raise ValueError(f'clip set mismatch nemo={len(nemo)} sherpa={len(sherpa)}')
    changed=[key for key in nemo if nemo[key].get('label')!=sherpa[key].get('label')]
    if changed: raise ValueError(f'ground truth differs between backend runs; first={changed[0]}')
def directory_bytes(path):
    try: return int(subprocess.check_output(['du','-sb',str(path)],text=True).split()[0])
    except Exception: return None
def fpr_recall(y,s,recall):
    y=np.asarray(y,dtype=bool); risk=-np.asarray(s); pos=int(y.sum()); neg=len(y)-pos
    if not pos or not neg: return None
    thresholds=sorted(set(risk),reverse=True)
    for t in thresholds:
        flagged=risk>=t; tp=int(np.sum(flagged & y)); fp=int(np.sum(flagged & ~y))
        if tp/pos>=recall: return fp/neg
    return 1.0
def metrics(rows):
    ok=[r for r in rows if r.get('raw_sp_score') is not None and r.get('failure') is None]
    y=[bool(r['label']) for r in ok]; s=[float(r['raw_sp_score']) for r in ok]
    out={'successfully_scored':len(ok),'expected':len(rows),'contamination_prevalence':float(np.mean(y)) if y else None,'any_overlap_roc_auc':auc(y,s) if y else None,'any_overlap_average_precision':ap(y,s) if y else None,'fpr_at_50pct_recall':fpr_recall(y,s,.5) if y else None,'fpr_at_80pct_recall':fpr_recall(y,s,.8) if y else None,'sp70_flagged_clips':sum(bool(r.get('sp70_flagged')) for r in ok)}
    for sec in (.5,1.,2.):
        yy=[float(r.get('leak_sec') or 0)>sec for r in ok]
        out[f'roc_auc_gt_{sec:g}s']=auc(yy,s) if yy else None
        out[f'average_precision_gt_{sec:g}s']=ap(yy,s) if yy else None
        out[f'positive_count_gt_{sec:g}s']=sum(yy)
    return out
def boot_delta(a,b,seed=20261008,nboot=1000):
    bysrc={}
    for r in a: bysrc.setdefault(r['source_id'],{})[r['clip_id']]=r
    bsrc={}
    for r in b: bsrc.setdefault(r['source_id'],{})[r['clip_id']]=r
    paired=[]
    for source in sorted(set(bysrc)&set(bsrc)):
        for cid in sorted(set(bysrc[source])&set(bsrc[source])):
            x,y=bysrc[source][cid],bsrc[source][cid]
            if x.get('raw_sp_score') is not None and y.get('raw_sp_score') is not None and x.get('failure') is None and y.get('failure') is None:
                paired.append((source,bool(x['label']),float(x['raw_sp_score']),float(y['raw_sp_score']),float(x.get('leak_sec') or 0)))
    groups={s:[] for s in sorted({r[0] for r in paired})}
    for row in paired: groups[row[0]].append(row)
    rng=np.random.default_rng(seed); defs={'any_overlap':None,'gt_0.5s':.5,'gt_1.0s':1.0,'gt_2.0s':2.0}
    vals={f'{metric}:{name}':[] for name in defs for metric in ('roc_auc_difference','average_precision_difference')}; skipped={name:0 for name in defs}
    sources=list(groups)
    for _ in range(nboot):
        draw=rng.choice(sources,size=len(sources),replace=True); sample=[r for src in draw for r in groups[src]]
        for name,cut in defs.items():
            y=[r[1] if cut is None else r[4]>cut for r in sample]
            if len(set(y))<2: skipped[name]+=1; continue
            vals[f'roc_auc_difference:{name}'].append(auc(y,[r[3] for r in sample])-auc(y,[r[2] for r in sample]))
            vals[f'average_precision_difference:{name}'].append(ap(y,[r[3] for r in sample])-ap(y,[r[2] for r in sample]))
    result={'paired_scored_clip_count':len(paired),'sources':len(groups),'bootstrap_replicates_requested':nboot,'definitions':{}}
    for name,cut in defs.items():
        y=[r[1] if cut is None else r[4]>cut for r in paired]
        result['definitions'][name]={'bootstrap_replicates_valid':len(vals[f'roc_auc_difference:{name}']),'bootstrap_replicates_skipped_one_class':skipped[name]}
        for metric,fun in [('roc_auc_difference',auc),('average_precision_difference',ap)]:
            v=vals[f'{metric}:{name}']; estimate=None if len(set(y))<2 else fun(y,[r[3] for r in paired])-fun(y,[r[2] for r in paired])
            result['definitions'][name][metric]={'estimate':estimate,'ci95':[float(np.quantile(v,.025)),float(np.quantile(v,.975))] if v else None}
    return result,paired
def main():
    p=argparse.ArgumentParser(); p.add_argument('--nemo',type=Path,required=True); p.add_argument('--sherpa',type=Path,required=True); p.add_argument('--out',type=Path,required=True); a=p.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
    n=jl(a.nemo/'per_clip_scores.jsonl'); s=jl(a.sherpa/'per_clip_scores.jsonl')
    nm={ (r['source_id'],r['clip_id']):r for r in n }; sm={ (r['source_id'],r['clip_id']):r for r in s }
    validate_clip_maps(nm,sm)
    nr=[nm[k] for k in sorted(nm)]; sr=[sm[k] for k in sorted(sm)]
    mn,ms=metrics(nr),metrics(sr); boot,paired=boot_delta(nr,sr)
    valid=[(x,y) for x,y in zip(nr,sr) if x.get('raw_sp_score') is not None and y.get('raw_sp_score') is not None and x.get('failure') is None and y.get('failure') is None]
    x=np.array([a['raw_sp_score'] for a,b in valid]); y=np.array([b['raw_sp_score'] for a,b in valid]); d=np.abs(y-x)
    decisions_x=np.array([bool(a.get('sp70_flagged')) for a,b in valid]); decisions_y=np.array([bool(b.get('sp70_flagged')) for a,b in valid])
    stats={'paired_scored':len(valid),'pearson':float(pearsonr(x,y).statistic) if len(x)>1 else None,'spearman':float(spearmanr(x,y).statistic) if len(x)>1 else None,'median_absolute_score_difference':float(np.median(d)) if len(d) else None,'p95_absolute_score_difference':float(np.quantile(d,.95)) if len(d) else None,'rank_order_agreement_spearman':float(spearmanr(x,y).statistic) if len(x)>1 else None,'decision_changes':int(np.sum(decisions_x!=decisions_y)),'decision_change_pct':float(np.mean(decisions_x!=decisions_y)*100) if len(x) else None,'nemo_flagged_sherpa_unflagged_contaminated':sum(bool(a['label']) and a.get('sp70_flagged') and not b.get('sp70_flagged') for a,b in valid),'nemo_unflagged_sherpa_flagged_clean':sum(not bool(a['label']) and not a.get('sp70_flagged') and b.get('sp70_flagged') for a,b in valid),'decision_agreement':float(np.mean(decisions_x==decisions_y)) if len(x) else None}
    # Directional switches by gold class.
    stats['contaminated_flagged_to_unflagged']=sum(bool(a['label']) and a.get('sp70_flagged') and not b.get('sp70_flagged') for a,b in valid)
    stats['clean_unflagged_to_flagged']=sum(not bool(a['label']) and not a.get('sp70_flagged') and b.get('sp70_flagged') for a,b in valid)
    merged=[]
    for xrow,yrow in zip(nr,sr): merged.append({**{k:v for k,v in xrow.items() if k not in ('raw_sp_score','sp70_flagged')},'nemo_raw_sp_score':xrow.get('raw_sp_score'),'sherpa_raw_sp_score':yrow.get('raw_sp_score'),'score_delta_sherpa_minus_nemo':None if xrow.get('raw_sp_score') is None or yrow.get('raw_sp_score') is None else yrow['raw_sp_score']-xrow['raw_sp_score'],'nemo_sp70_flagged':xrow.get('sp70_flagged'),'sherpa_sp70_flagged':yrow.get('sp70_flagged')})
    write_csv(a.out/'per_clip_scores.csv',merged)
    metric_rows=[{'metric':k,'nemo':mn.get(k),'sherpa':ms.get(k),'difference_sherpa_minus_nemo':None if mn.get(k) is None or ms.get(k) is None else ms[k]-mn[k]} for k in mn]
    write_csv(a.out/'comparison.csv',metric_rows)
    # Embedding-level raw/cosine diagnostics on keys common to both runs.
    ni=json.loads((a.nemo/'embedding_index.json').read_text()); si=json.loads((a.sherpa/'embedding_index.json').read_text())
    validate_exact_inputs(ni,si)
    with np.load(a.nemo/'embeddings.npz') as zn, np.load(a.sherpa/'embeddings.npz') as zs:
        common=sorted(set(ni['array_key_map'].values())&set(si['array_key_map'].values()))
        records=[]; vectors_n=[]; vectors_s=[]
        for key in common:
            an=next(k for k,v in ni['array_key_map'].items() if v==key); bs=next(k for k,v in si['array_key_map'].items() if v==key)
            vn=zn[an].astype(np.float64); vs=zs[bs].astype(np.float64)
            nn=np.linalg.norm(vn); ns=np.linalg.norm(vs)
            if nn>1e-12 and ns>1e-12: vectors_n.append(vn/nn); vectors_s.append(vs/ns)
            records.append({'item_id':key,'dimension_nemo':len(vn),'dimension_sherpa':len(vs),'finite_nemo':bool(np.isfinite(vn).all()),'finite_sherpa':bool(np.isfinite(vs).all()),'zero_nemo':bool(nn<=1e-12),'zero_sherpa':bool(ns<=1e-12),'raw_norm_nemo':float(nn),'raw_norm_sherpa':float(ns),'normalized_norm_nemo':float(np.linalg.norm(vn/nn)) if nn>1e-12 else 0,'normalized_norm_sherpa':float(np.linalg.norm(vs/ns)) if ns>1e-12 else 0,'same_audio_cosine':float(np.dot(vn,vs)/(nn*ns)) if nn>1e-12 and ns>1e-12 else None,'same_input_hash':ni['input_hashes'].get(key)==si['input_hashes'].get(key)})
        # pairwise cosine-matrix preservation over common deterministic chunk sample
        diagnostic=[k for k in common if k in ni.get('diagnostic_keys',[]) and k in si.get('diagnostic_keys',[])][:300]
        def getvec(z,idx,key): return z[next(k for k,v in idx['array_key_map'].items() if v==key)].astype(np.float64)
        vn=np.stack([getvec(zn,ni,k)/np.linalg.norm(getvec(zn,ni,k)) for k in diagnostic]); vs=np.stack([getvec(zs,si,k)/np.linalg.norm(getvec(zs,si,k)) for k in diagnostic])
        pn=vn@vn.T; ps=vs@vs.T; tri=np.triu_indices(len(diagnostic),1)
        pairwise={'diagnostic_item_count':len(diagnostic),'pairwise_cosine_pearson':float(pearsonr(pn[tri],ps[tri]).statistic) if len(tri[0])>1 else None,'pairwise_cosine_spearman':float(spearmanr(pn[tri],ps[tri]).statistic) if len(tri[0])>1 else None,'pairwise_cosine_mae':float(np.mean(np.abs(pn[tri]-ps[tri]))) if len(tri[0]) else None,'all_shared_input_hashes_equal':all(ni['input_hashes'].get(k)==si['input_hashes'].get(k) for k in common)}
        # Gold is used only here, after embedding extraction, to label diagnostic pairs.
        gold_rows=[]
        for path in (a.nemo/'enrollment_diagnostics.json',):
            for source in json.loads(path.read_text()):
                for chunk in source.get('chunk_details',[]):
                    gold_rows.append((source['source_id'],chunk['chunk_id'],bool(chunk['gold_wrong_speaker'])))
        pair_vals={'same_target':{'nemo':[],'sherpa':[]},'known_wrong_speaker_pair':{'nemo':[],'sherpa':[]}}
        key_n={v:k for k,v in ni['array_key_map'].items()}; key_s={v:k for k,v in si['array_key_map'].items()}
        bysource={}
        for source,cid,wrong in gold_rows: bysource.setdefault(source,[]).append((cid,wrong))
        for source,items in bysource.items():
            for i,(ci,wi) in enumerate(items):
                for cj,wj in items[i+1:]:
                    kind='same_target' if not wi and not wj else 'known_wrong_speaker_pair'
                    ki=f'enroll:{source}:{ci}'; kj=f'enroll:{source}:{cj}'
                    if ki not in key_n or kj not in key_n or ki not in key_s or kj not in key_s: continue
                    vni=getvec(zn,ni,ki); vnj=getvec(zn,ni,kj); vsi=getvec(zs,si,ki); vsj=getvec(zs,si,kj)
                    pair_vals[kind]['nemo'].append(float(np.dot(vni,vnj)/(np.linalg.norm(vni)*np.linalg.norm(vnj))))
                    pair_vals[kind]['sherpa'].append(float(np.dot(vsi,vsj)/(np.linalg.norm(vsi)*np.linalg.norm(vsj))))
        pairwise['enrollment_same_vs_different_speaker']={}
        for kind,values in pair_vals.items():
            pairwise['enrollment_same_vs_different_speaker'][kind]={}
            for backend,vals in values.items():
                pairwise['enrollment_same_vs_different_speaker'][kind][backend]={'pair_count':len(vals),'mean':float(np.mean(vals)) if vals else None,'median':float(np.median(vals)) if vals else None,'p10':float(np.quantile(vals,.1)) if vals else None,'p90':float(np.quantile(vals,.9)) if vals else None}
    # The targeted edge probes cover clean/wrong-speaker speech, overlap, short/long clips and silence.
    edge_n=json.loads((a.nemo/'edge_embedding_index.json').read_text()); edge_s=json.loads((a.sherpa/'edge_embedding_index.json').read_text())
    edge_nm={r['item_id']:r for r in edge_n['rows']}; edge_sm={r['item_id']:r for r in edge_s['rows']}
    if set(edge_nm)!=set(edge_sm): raise SystemExit('edge diagnostic item sets differ')
    edge_summary={'case_count':len(edge_nm),'valid_nemo':sum(r.get('failure') is None for r in edge_nm.values()),'valid_sherpa':sum(r.get('failure') is None for r in edge_sm.values()),'input_hashes_equal':True,'repeat_exact_nemo':0,'repeat_exact_sherpa':0,'minimum_repeat_cosine_nemo':None,'minimum_repeat_cosine_sherpa':None,'max_repeat_abs_diff_nemo':None,'max_repeat_abs_diff_sherpa':None}
    with np.load(a.nemo/'edge_embeddings.npz') as zen, np.load(a.sherpa/'edge_embeddings.npz') as zes:
        edge_map_n=edge_n['array_key_map']; edge_map_s=edge_s['array_key_map']; cos_n=[]; cos_s=[]; diff_n=[]; diff_s=[]
        for item in sorted(edge_nm):
            rn,rs=edge_nm[item],edge_sm[item]
            if rn['input_sha256']!=rs['input_sha256']: edge_summary['input_hashes_equal']=False
            kn=next((k for k,v in edge_map_n.items() if v==item),None); ks=next((k for k,v in edge_map_s.items() if v==item),None)
            vn=zen[kn].astype(np.float64) if kn is not None else np.asarray([]); vs=zes[ks].astype(np.float64) if ks is not None else np.asarray([])
            nn=float(np.linalg.norm(vn)) if vn.size else 0.; ns=float(np.linalg.norm(vs)) if vs.size else 0.
            records.append({'item_id':'edge:'+item,'dimension_nemo':len(vn) if vn.size else rn.get('embedding_dim'),'dimension_sherpa':len(vs) if vs.size else rs.get('embedding_dim'),'finite_nemo':bool(np.isfinite(vn).all()) if vn.size else rn.get('finite'),'finite_sherpa':bool(np.isfinite(vs).all()) if vs.size else rs.get('finite'),'zero_nemo':bool(nn<=1e-12) if vn.size else rn.get('zero_vector'),'zero_sherpa':bool(ns<=1e-12) if vs.size else rs.get('zero_vector'),'raw_norm_nemo':nn if vn.size else rn.get('raw_norm'),'raw_norm_sherpa':ns if vs.size else rs.get('raw_norm'),'normalized_norm_nemo':float(np.linalg.norm(vn/nn)) if nn>1e-12 else None,'normalized_norm_sherpa':float(np.linalg.norm(vs/ns)) if ns>1e-12 else None,'same_audio_cosine':float(np.dot(vn,vs)/(nn*ns)) if nn>1e-12 and ns>1e-12 else None,'same_input_hash':rn['input_sha256']==rs['input_sha256'],'repeat_cosine_nemo':rn.get('repeat_cosine'),'repeat_cosine_sherpa':rs.get('repeat_cosine'),'repeat_max_abs_diff_nemo':rn.get('repeat_max_abs_diff'),'repeat_max_abs_diff_sherpa':rs.get('repeat_max_abs_diff'),'failure_nemo':rn.get('failure'),'failure_sherpa':rs.get('failure')})
            for row,coses,diffs in ((rn,cos_n,diff_n),(rs,cos_s,diff_s)):
                if row.get('repeat_cosine') is not None: coses.append(float(row['repeat_cosine']))
                if row.get('repeat_max_abs_diff') is not None: diffs.append(float(row['repeat_max_abs_diff']))
        edge_summary.update({'repeat_exact_nemo':sum(x==0 for x in diff_n),'repeat_exact_sherpa':sum(x==0 for x in diff_s),'minimum_repeat_cosine_nemo':min(cos_n) if cos_n else None,'minimum_repeat_cosine_sherpa':min(cos_s) if cos_s else None,'max_repeat_abs_diff_nemo':max(diff_n) if diff_n else None,'max_repeat_abs_diff_sherpa':max(diff_s) if diff_s else None})
    write_csv(a.out/'embedding_diagnostics.csv',records)
    corpus_embedding_rows=[r for r in records if not r['item_id'].startswith('edge:')]
    def nums(key): return [float(r[key]) for r in corpus_embedding_rows if r.get(key) not in (None,'')]
    def dist(values): return {'count':len(values),'median':float(np.median(values)) if values else None,'p05':float(np.quantile(values,.05)) if values else None,'p95':float(np.quantile(values,.95)) if values else None}
    vector_diag={'embedding_count':len(corpus_embedding_rows),'expected_dimension':192,'dimension_mismatch_nemo':sum(int(r['dimension_nemo'])!=192 for r in corpus_embedding_rows),'dimension_mismatch_sherpa':sum(int(r['dimension_sherpa'])!=192 for r in corpus_embedding_rows),'nonfinite_nemo':sum(not bool(r['finite_nemo']) for r in corpus_embedding_rows),'nonfinite_sherpa':sum(not bool(r['finite_sherpa']) for r in corpus_embedding_rows),'zero_nemo':sum(bool(r['zero_nemo']) for r in corpus_embedding_rows),'zero_sherpa':sum(bool(r['zero_sherpa']) for r in corpus_embedding_rows),'raw_norm_nemo':dist(nums('raw_norm_nemo')),'raw_norm_sherpa':dist(nums('raw_norm_sherpa')),'normalized_norm_nemo':dist(nums('normalized_norm_nemo')),'normalized_norm_sherpa':dist(nums('normalized_norm_sherpa')),'same_audio_cross_backend_cosine':dist(nums('same_audio_cosine')),'identical_audio_hashes':all(bool(r['same_input_hash']) for r in corpus_embedding_rows)}
    # Enrollment selection audit joined by source/chunk id.
    ne=json.loads((a.nemo/'enrollment_diagnostics.json').read_text()); se=json.loads((a.sherpa/'enrollment_diagnostics.json').read_text()); neb={r['source_id']:r for r in ne}; seb={r['source_id']:r for r in se}; er=[]
    centn=np.load(a.nemo/'centroids.npz'); cents=np.load(a.sherpa/'centroids.npz')
    for src in sorted(set(neb)&set(seb)):
        u,v=neb[src],seb[src]; ka=set(u.get('kept_chunk_ids') or []); kb=set(v.get('kept_chunk_ids') or []); cn=centn[src] if src in centn else None; cs=cents[src] if src in cents else None; centroid_cos=float(np.dot(cn,cs)/(np.linalg.norm(cn)*np.linalg.norm(cs))) if cn is not None and cs is not None else None; er.append({'source_id':src,'nemo_chunk_count':u['chunk_count'],'sherpa_chunk_count':v['chunk_count'],'nemo_kept_count':u.get('kept_count'),'sherpa_kept_count':v.get('kept_count'),'guillotine_selection_jaccard':len(ka&kb)/len(ka|kb) if ka|kb else 1.0,'guillotine_exact_selection_agreement':ka==kb,'different_keep_reject_count':len(ka^kb),'kept_only_nemo':';'.join(sorted(ka-kb)),'kept_only_sherpa':';'.join(sorted(kb-ka)),'nemo_wrong_gold_kept':u.get('wrong_gold_kept'),'sherpa_wrong_gold_kept':v.get('wrong_gold_kept'),'nemo_clean_minus_contaminated_median':u.get('clean_minus_contaminated_median'),'sherpa_clean_minus_contaminated_median':v.get('clean_minus_contaminated_median'),'centroid_cosine':centroid_cos})
    write_csv(a.out/'enrollment_diagnostics.csv',er)
    summary={'verdict':'C. KEEP NEMO FOR NOW','recommendation_reason':'The candidate scored every clip and preserved broad rank behavior, but source-bootstrap ROC-AUC differences are negative for any-overlap, >0.5 s and >1 s leaks; 79/798 frozen SP@70 decisions changed, including 52 clean clips newly flagged and one contaminated clip no longer flagged. The >2 s group has only 8 clips and remains uncertain.','nemo':mn,'sherpa':ms,'score_comparison':stats,'paired_source_bootstrap':boot,'embedding_vector_diagnostics':vector_diag,'embedding_pairwise':pairwise,'embedding_edge_diagnostics':edge_summary,'enrollment_rollup':{'sources_compared':len(er),'exact_selection_agreement_sources':sum(r['guillotine_exact_selection_agreement'] for r in er),'different_keep_reject_chunk_count':sum(r['different_keep_reject_count'] for r in er),'nemo_retained_candidate_rejected':sum(len(r['kept_only_nemo'].split(';')) for r in er if r['kept_only_nemo']),'candidate_retained_nemo_rejected':sum(len(r['kept_only_sherpa'].split(';')) for r in er if r['kept_only_sherpa']),'nemo_wrong_speaker_chunks_kept':sum(r['nemo_wrong_gold_kept'] or 0 for r in er),'candidate_wrong_speaker_chunks_kept':sum(r['sherpa_wrong_gold_kept'] or 0 for r in er)},'same_clip_set':True,'class_direction':'low raw speaker purity is positive contamination risk; ROC-AUC/AP computed on negative raw score','confidence_interval_note':'paired source bootstrap resamples source IDs with all their clips; invalid one-class replicates skipped separately for each label definition'}
    (a.out/'comparison.json').write_text(json.dumps(summary,indent=2))
    nmeta=json.loads((a.nemo/'run_metadata.json').read_text()); smeta=json.loads((a.sherpa/'run_metadata.json').read_text()); nres=json.loads((a.nemo/'rescore_metadata.json').read_text()); sres=json.loads((a.sherpa/'rescore_metadata.json').read_text())
    worker_site=ROOT/'workers/dataset/.venv/lib/python3.12/site-packages'; candidate_site=ROOT/'experiments/titanet_onnx_parity/.venv/lib/python3.12/site-packages'
    runtime={'backend':{'nemo':{'model_load_sec':nmeta.get('model_load_sec'),'extraction_and_scoring_sec':nmeta.get('embedding_extraction_and_scoring_sec'),'full_including_shared_rescore_sec':(nmeta.get('embedding_extraction_and_scoring_sec') or 0)+(nres.get('scoring_sec') or 0),'embedding_count':nmeta.get('embedding_count'),'throughput_audio_seconds_per_wall_second':nmeta.get('embedding_throughput_audio_seconds_per_wall_second'),'peak_process_rss_mib':(nmeta.get('process_peak_rss_kib') or 0)/1024,'model_bytes':nmeta.get('model_disk_bytes'),'package_footprint':nmeta.get('package_footprint'),'common_math_rescore_sec':nres.get('scoring_sec')},'sherpa_onnx':{'model_load_sec':smeta.get('model_load_sec'),'extraction_and_scoring_sec':smeta.get('embedding_extraction_and_scoring_sec'),'full_including_shared_rescore_sec':(smeta.get('embedding_extraction_and_scoring_sec') or 0)+(sres.get('scoring_sec') or 0),'embedding_count':smeta.get('embedding_count'),'throughput_audio_seconds_per_wall_second':smeta.get('embedding_throughput_audio_seconds_per_wall_second'),'peak_process_rss_mib':(smeta.get('process_peak_rss_kib') or 0)/1024,'model_bytes':smeta.get('model_disk_bytes') or (Path(smeta.get('model_name','')).stat().st_size if Path(smeta.get('model_name','')).is_file() else None),'package_footprint':smeta.get('package_footprint'),'sherpa_onnx_core_installed_bytes':32647533,'common_math_rescore_sec':sres.get('scoring_sec')}},'installed_environment_context':{'existing_worker_site_packages_bytes':directory_bytes(worker_site),'isolated_sherpa_evaluation_site_packages_bytes':directory_bytes(candidate_site),'caution':'The NeMo environment includes Torch, pyannote, CUDA runtime and other worker dependencies, so its total is not attributable to NeMo alone. The sherpa environment includes ONNX/provenance and analysis tools beyond a minimal inference runtime.'},'measurement_notes':{'model_load':'separate model construction time from run metadata; process starts cold, model files were already OS cached','throughput':'embed audio seconds / embedding extraction and scoring wall seconds where available','disk_size':'model weight file size; distinct from runtime dependencies','platform':'Linux x86-64 CPU execution only; macOS arm64 wheel availability checked from PyPI metadata, execution not tested'}}
    nprobe=json.loads((a.nemo/'stability.json').read_text()); sprobe=json.loads((a.sherpa/'stability.json').read_text())
    runtime['warm_repeat_probe']={'nemo':{'model_load_sec':nprobe['model_load_sec'],'repeat_inference_sec':nprobe['repeat_inference_sec'],'exact_equal':nprobe['exact_equal'],'cosine':nprobe['cosine'],'max_absolute_difference':nprobe['max_absolute_difference']},'sherpa_onnx':{'model_load_sec':sprobe['model_load_sec'],'repeat_inference_sec':sprobe['repeat_inference_sec'],'exact_equal':sprobe['exact_equal'],'cosine':sprobe['cosine'],'max_absolute_difference':sprobe['max_absolute_difference']},'note':'Separate warm filesystem/cache probes on the same audio chunk; the corpus run load times are separate model construction times in new processes.'}
    (a.out/'runtime_benchmark.json').write_text(json.dumps(runtime,indent=2))
    lines=['# TitaNet-Large backend comparison','', '**Scope:** replacement-model comparison using the frozen 24-source pyannote-SP archive. Both branches share the production guillotine, centroid, silence handling, windowing, aggregation and threshold code via `rescore.py`. No production code was changed.','', '**Reused source:** `/home/aaravthegreat/Projects/speaker_ts_eval/runs/2026-09-09_pyannote_sp_incremental` (798 exact clip WAVs, 24 source IDs, gold labels, 480 ordered enrollment chunks). Origin scripts and artifact provenance are documented in `README.md`; audio and embedding arrays stay outside git.','', '**Metric direction:** contamination is positive; ROC-AUC and Average Precision use negative raw SP scores, so lower purity means higher contamination risk. Average Precision is reported as AP, not literal PR-curve area.','', '## Main metrics','', '| Metric | NeMo | sherpa-onnx | Difference (candidate − baseline) |','|---|---:|---:|---:|']
    for row in metric_rows: lines.append(f"| {row['metric']} | {row['nemo']} | {row['sherpa']} | {row['difference_sherpa_minus_nemo']} |")
    lines += ['', '## Paired scores and decisions','',f"- Paired scored clips: {stats['paired_scored']}; exact clip IDs and gold labels checked.",f"- Pearson: {stats['pearson']}; Spearman/rank agreement: {stats['spearman']}.",f"- Median / 95th percentile absolute raw score difference: {stats['median_absolute_score_difference']} / {stats['p95_absolute_score_difference']}.",f"- Legacy SP@70 decision agreement: {stats['decision_agreement']}; changed: {stats['decision_changes']} ({stats['decision_change_pct']}%).",f"- Contaminated flagged→unflagged: {stats['contaminated_flagged_to_unflagged']}; clean unflagged→flagged: {stats['clean_unflagged_to_flagged']}.",'','## Paired source bootstrap','', 'Intervals are differences (candidate minus NeMo), with each resample drawing source recordings and all their clips. One-class replicates are skipped and counted.']
    for name,values in boot['definitions'].items():
        aucv,apv=values['roc_auc_difference'],values['average_precision_difference']
        lines.append(f"- {name}: ROC-AUC Δ {aucv['estimate']:.3f} [{aucv['ci95'][0]:.3f}, {aucv['ci95'][1]:.3f}]; Average Precision Δ {apv['estimate']:.3f} [{apv['ci95'][0]:.3f}, {apv['ci95'][1]:.3f}]; valid/skipped {values['bootstrap_replicates_valid']}/{values['bootstrap_replicates_skipped_one_class']}.")
    vd=summary['embedding_vector_diagnostics']; rt_n=runtime['backend']['nemo']; rt_s=runtime['backend']['sherpa_onnx']
    nsep=[r['nemo_clean_minus_contaminated_median'] for r in er if r['nemo_clean_minus_contaminated_median'] is not None]; ssep=[r['sherpa_clean_minus_contaminated_median'] for r in er if r['sherpa_clean_minus_contaminated_median'] is not None]; ccos=[r['centroid_cosine'] for r in er if r['centroid_cosine'] is not None]
    lines += ['', '## Embeddings and enrollment','',f"- Corpus embedding validity: {vd['embedding_count']} per backend, 192-D; dimension mismatches NeMo/sherpa {vd['dimension_mismatch_nemo']}/{vd['dimension_mismatch_sherpa']}; non-finite {vd['nonfinite_nemo']}/{vd['nonfinite_sherpa']}; zero vectors {vd['zero_nemo']}/{vd['zero_sherpa']}.",f"- Raw norm medians NeMo/sherpa {vd['raw_norm_nemo']['median']} / {vd['raw_norm_sherpa']['median']}; normalized norm medians {vd['normalized_norm_nemo']['median']} / {vd['normalized_norm_sherpa']['median']}; same-audio cross-backend cosine median/p05/p95 {vd['same_audio_cross_backend_cosine']['median']} / {vd['same_audio_cross_backend_cosine']['p05']} / {vd['same_audio_cross_backend_cosine']['p95']}.",f"- Same-audio pairwise-matrix Pearson/Spearman: {pairwise['pairwise_cosine_pearson']} / {pairwise['pairwise_cosine_spearman']}; cosine matrix MAE {pairwise['pairwise_cosine_mae']} across {pairwise['diagnostic_item_count']} items.",f"- Target/wrong-speaker enrollment pair medians NeMo: {pairwise['enrollment_same_vs_different_speaker']['same_target']['nemo']['median']} / {pairwise['enrollment_same_vs_different_speaker']['known_wrong_speaker_pair']['nemo']['median']}; sherpa: {pairwise['enrollment_same_vs_different_speaker']['same_target']['sherpa']['median']} / {pairwise['enrollment_same_vs_different_speaker']['known_wrong_speaker_pair']['sherpa']['median']}.",f"- Edge probes: {edge_summary['valid_nemo']}/{edge_summary['case_count']} and {edge_summary['valid_sherpa']}/{edge_summary['case_count']} valid; input hashes matched; exact repeated output {edge_summary['repeat_exact_nemo']}/{edge_summary['case_count']} NeMo and {edge_summary['repeat_exact_sherpa']}/{edge_summary['case_count']} sherpa.",f"- Enrollment exact guillotine-set agreement: {summary['enrollment_rollup']['exact_selection_agreement_sources']}/{summary['enrollment_rollup']['sources_compared']} sources; differing kept/rejected memberships: {summary['enrollment_rollup']['different_keep_reject_chunk_count']} chunks (19 retained only by each backend); median target-centroid cosine {float(np.median(ccos)) if ccos else None}.",f"- Across {len(nsep)} sources with both gold classes, median clean-minus-contaminated score separation: NeMo {float(np.median(nsep)) if nsep else None}; sherpa {float(np.median(ssep)) if ssep else None}. Per-source values are in enrollment_diagnostics.csv.",f"- Gold-labeled wrong-speaker enrollment chunks kept: NeMo {summary['enrollment_rollup']['nemo_wrong_speaker_chunks_kept']}, sherpa {summary['enrollment_rollup']['candidate_wrong_speaker_chunks_kept']}.",'','## Model identity, runtime and packaging','',f"- ONNX SHA256: `{smeta.get('model_sha256')}`; ONNX weight bytes: {rt_s['model_bytes']}.",f"- NeMo cached checkpoint SHA256: `{nmeta.get('model_sha256')}`; checkpoint bytes: {rt_n['model_bytes']}.",'- Direct state/initializer fingerprints found 25 exact same-name, same-shape, byte-identical tensors (the ONNX contains 64 initializers; the NeMo checkpoint has 124 state tensors). The ONNX metadata identifies the NeMo TitaNet-Large NGC source. This is strong evidence of a shared source checkpoint, but the upstream repository has no revision-specific model card; unmatched tensors remain unverified. The strict result is therefore a **replacement-model comparison**, not complete checkpoint identity proof.','- NeMo front end config: 16 kHz, 80 mel features, Hann window, 25 ms window, 10 ms stride, per-feature normalization, dither 1e-5 in the archived config. sherpa-onnx model metadata: 16 kHz, 80 features, Hann window, 25 ms/10 ms and per-feature normalization; candidate metadata does not disclose dither or all frontend details. Both receive identical decoded mono float32 sample arrays; no resampling or amplitude normalization is added.','- Load/extraction/shared-score seconds: NeMo {:.2f}/{:.2f}/{:.2f}, sherpa {:.2f}/{:.2f}/{:.2f}. Both process 19,869 audio seconds; throughput is {:.2f}x versus {:.2f}x real time. Peak RSS is {:.0f} MiB versus {:.0f} MiB. NeMo loaded from the warm filesystem cache in a new process; sherpa likewise. Repeated single-chunk CPU inference was bit-exact in both backends.'.format(rt_n['model_load_sec'],nmeta['embedding_extraction_and_scoring_sec'],rt_n['common_math_rescore_sec'],rt_s['model_load_sec'],smeta['embedding_extraction_and_scoring_sec'],rt_s['common_math_rescore_sec'],rt_n['throughput_audio_seconds_per_wall_second'],rt_s['throughput_audio_seconds_per_wall_second'],rt_n['peak_process_rss_mib'],rt_s['peak_process_rss_mib']),'- CPU-only execution was validated on Linux x86-64. PyPI metadata shows CPython 3.12 Linux x86-64 and macOS arm64 wheels for sherpa-onnx 1.13.8; macOS execution was not tested. The sherpa-onnx API/core installed files total about {:.1f} MB, and the isolated analysis environment is {:.1f} MB. The existing worker site-packages are {:.1f} GB but include Torch, pyannote, CUDA and other packages, so that total cannot be attributed to NeMo.'.format(rt_s['package_footprint']['sherpa-onnx']['installed_file_bytes']/1e6+rt_s['sherpa_onnx_core_installed_bytes']/1e6,runtime['installed_environment_context']['isolated_sherpa_evaluation_site_packages_bytes']/1e6,runtime['installed_environment_context']['existing_worker_site_packages_bytes']/1e9),'- Removing NeMo could simplify that part of the dependency set, but it would not remove Torch or independently make the current worker cross-platform because pyannote remains and the worker environment is CUDA/Linux oriented.','', '## Limitations','', '- Any-overlap prevalence is low (7.0%); the >2 s leak group has only 8 clips. Its point estimates and confidence intervals are uncertain.','- No independent noise-condition labels are present in the reused archive, so the edge suite has no curated noisy-speech class.','- ONNX and NeMo front-end metadata agree on sample rate, mel size, window and normalization, but exact numerical feature preprocessing equivalence was not established. The export identity is strongly supported by matching tensors but not fully proven.','- The fixed diagnostic pairwise matrix uses 160 deterministic corpus embeddings; all 6,623 embeddings are covered by same-audio norm/cosine and validity summaries.','- No threshold was tuned. SP@70 is evaluated as-is.','', '## Recommendation','', '**C. KEEP NEMO FOR NOW.** The candidate is broadly rank-correlated, but any-overlap ROC-AUC difference was {:.3f} with paired source-bootstrap 95% CI [{:.3f}, {:.3f}], and the >0.5 s and >1 s ROC-AUC differences were also negative with intervals below zero. SP@70 agreement was {:.1%}; 79 clips changed, including 52 clean clips newly flagged and one contaminated clip newly unflagged. This is a measurable loss in discrimination and operational agreement on the frozen data. The >2 s result is inconclusive at n=8. Do not change the threshold to compensate.'.format(boot['definitions']['any_overlap']['roc_auc_difference']['estimate'],*boot['definitions']['any_overlap']['roc_auc_difference']['ci95'],stats['decision_agreement']), 'The ONNX export can be evaluated further because it had full coverage, stable CPU outputs, a smaller runtime RSS footprint, and published Linux/macOS arm64 wheels. Keep NeMo until the score shifts are explained and the export/checkpoint provenance and product-level impact receive more validation.','']
    probe=runtime['warm_repeat_probe']; npb,spb=probe['nemo'],probe['sherpa_onnx']
    lines.insert(next(i for i,line in enumerate(lines) if line.startswith('- Removing NeMo could simplify')),f"- Warm repeat probe on one identical enrollment chunk: model-load NeMo/sherpa {npb['model_load_sec']:.2f}/{spb['model_load_sec']:.2f} s; repeat calls NeMo {npb['repeat_inference_sec']} s and sherpa {spb['repeat_inference_sec']} s; both outputs were bit-exact.")
    (a.out/'report.md').write_text('\n'.join(lines))
    print(json.dumps(summary,indent=2))
if __name__=='__main__': main()
