from __future__ import annotations
import itertools
import numpy as np

def describe(values: list[float]) -> dict:
    a=np.asarray(values,dtype=float)
    if not len(a): return {"count":0,"mean":None,"median":None,"std":None,"p10":None}
    return {"count":int(len(a)),"mean":float(a.mean()),"median":float(np.median(a)),"std":float(a.std(ddof=1)) if len(a)>1 else 0.0,"p10":float(np.percentile(a,10))}

def pairwise_cosine(vectors: dict[str, list[float]]) -> dict:
    pairs=[]
    keys=sorted(vectors)
    for a,b in itertools.combinations(keys,2):
        x=np.asarray(vectors[a],dtype=float); y=np.asarray(vectors[b],dtype=float)
        den=np.linalg.norm(x)*np.linalg.norm(y)
        if den: pairs.append(float(np.dot(x,y)/den))
    d=describe(pairs)
    return {"pair_count":d["count"],"mean":d["mean"],"std":d["std"],"p10":d["p10"],"note":None if pairs else "At least two valid distinct recordings are required."}

def speaker_similarity(real: dict[str,list[float]], generated: dict[str,list[float]]) -> dict[str,float]:
    if not real: return {}
    # Enrollment is the L2-normalized mean of all valid real recording embeddings.
    mat=np.asarray(list(real.values()),dtype=float)
    centroid=mat.mean(axis=0); norm=np.linalg.norm(centroid)
    if not norm: return {}
    centroid/=norm
    return {k:float(np.dot(centroid,np.asarray(v,dtype=float))/(np.linalg.norm(v) or 1.0)) for k,v in generated.items()}
