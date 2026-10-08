#!/usr/bin/env python3
"""Check deterministic repeated CPU extraction on the exact same enrolled chunk."""
import argparse, json, time
from pathlib import Path
import numpy as np
from run import Provider, read_audio

def main():
 p=argparse.ArgumentParser(); p.add_argument('--backend',choices=('nemo','sherpa'),required=True); p.add_argument('--audio',type=Path,required=True); p.add_argument('--out',type=Path,required=True); p.add_argument('--model',type=Path); a=p.parse_args()
 load=time.perf_counter(); provider=Provider(a.backend,a.model); load=time.perf_counter()-load
 samples=read_audio(a.audio); first=provider.embed(samples); second=provider.embed(samples)
 result={'backend':a.backend,'model_load_sec':load,'sample_sha256':__import__('hashlib').sha256(np.ascontiguousarray(samples,dtype=np.float32).tobytes()).hexdigest(),'embedding_dim':int(first.size),'finite':bool(np.isfinite(first).all() and np.isfinite(second).all()),'cosine':float(np.dot(first,second)/(np.linalg.norm(first)*np.linalg.norm(second))),'max_absolute_difference':float(np.max(np.abs(first-second))),'exact_equal':bool(np.array_equal(first,second)),'repeat_inference_sec':provider.inference_times}
 a.out.parent.mkdir(parents=True,exist_ok=True); a.out.write_text(json.dumps(result,indent=2)); print(json.dumps(result,indent=2))
if __name__=='__main__': main()
