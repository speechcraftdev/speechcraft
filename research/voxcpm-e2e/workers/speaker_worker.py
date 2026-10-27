"""SpeechBrain ECAPA embedding worker; uses upstream pretrained encoder."""
import json, sys
from pathlib import Path
import numpy as np
payload=json.load(sys.stdin)
backend=payload.get("backend","speechbrain")
model_id=payload.get("model","speechbrain/spkrec-ecapa-voxceleb")
revision=payload.get("revision","main")
license_id="Apache-2.0" if backend=="speechbrain" else "CC-BY-NC-4.0" if backend=="ecapa2" else "unknown"
out={"embeddings":{},"backend":backend,"model":model_id,"revision":revision,"license":license_id}
if payload.get("action")=="resolve":
    try:
        from huggingface_hub import snapshot_download, hf_hub_download
        if backend=="speechbrain": model_path=snapshot_download(repo_id=model_id,revision=revision,cache_dir=payload.get("model_cache"),local_files_only=bool(payload.get("local_files_only",False)))
        elif backend=="ecapa2": model_path=hf_hub_download(repo_id=model_id,filename="ecapa2.pt",revision=revision,cache_dir=payload.get("model_cache"))
        else: raise ValueError(f"Unsupported speaker backend: {backend!r}")
        out["resolved_revision"]=Path(model_path).parent.name if backend=="ecapa2" else Path(model_path).name
    except Exception as e: out["error"]=str(e)
    print(json.dumps(out)); sys.exit(0)
try:
    import torch, torchaudio, soundfile as sf
    if backend=="speechbrain":
        from speechbrain.inference.speaker import EncoderClassifier
        from huggingface_hub import snapshot_download
        model_path=snapshot_download(repo_id=model_id,revision=revision,cache_dir=payload.get("model_cache"),local_files_only=bool(payload.get("local_files_only",False)))
        out["resolved_revision"]=Path(model_path).name
        # The upstream hyperparams refer to the remote repository ID. Override
        # that with the already resolved local snapshot so SpeechBrain does not
        # perform a second, unpinned fetch for its four checkpoint files.
        classifier=EncoderClassifier.from_hparams(source=model_path,savedir=payload.get("model_cache"),overrides={"pretrained_path":model_path},huggingface_cache_dir=payload.get("model_cache"),run_opts={"device":payload.get("device","cpu")})
        device=payload.get("device","cpu")
        out["versions"]={"speechbrain":__import__("speechbrain").__version__,"torch":torch.__version__,"torchaudio":torchaudio.__version__,"cuda_build":torch.version.cuda,"cuda_available":torch.cuda.is_available(),"device":device,"cuda_device_name":torch.cuda.get_device_name(0) if device.startswith("cuda") and torch.cuda.is_available() else None}
    elif backend=="ecapa2":
        from huggingface_hub import hf_hub_download
        model_path=hf_hub_download(repo_id=model_id,filename="ecapa2.pt",revision=revision,cache_dir=payload.get("model_cache"))
        out["resolved_revision"]=Path(model_path).parent.name
        classifier=torch.jit.load(model_path,map_location=payload.get("device","cpu"))
        classifier.eval(); out["versions"]={"torch":torch.__version__,"checkpoint_file":"ecapa2.pt"}
    else: raise ValueError(f"Unsupported speaker backend: {backend!r}. Supported: speechbrain, ecapa2")
    for row in payload.get("items",[]):
        try:
            wav,sr=sf.read(row["path"],dtype="float32",always_2d=True); wav=wav.mean(axis=1)
            if wav.size == 0 or not np.isfinite(wav).all(): raise ValueError("audio must be nonempty and finite")
            wav=torch.as_tensor(np.ascontiguousarray(wav),dtype=torch.float32).unsqueeze(0)
            if sr!=16000: wav=torchaudio.functional.resample(wav,sr,16000)
            with torch.no_grad():
                if backend=="speechbrain": emb=classifier.encode_batch(wav)
                else: emb=classifier(wav)
            if isinstance(emb,(tuple,list)): emb=emb[0]
            if isinstance(emb,dict): emb=emb.get("embedding",next(iter(emb.values())))
            emb=emb.squeeze().cpu().numpy().astype(np.float32,copy=False)
            if emb.ndim != 1 or emb.size == 0 or not np.isfinite(emb).all(): raise ValueError("encoder returned an empty, non-vector, or non-finite embedding")
            norm=float(np.linalg.norm(emb))
            if not np.isfinite(norm) or norm <= 1e-12: raise ValueError("encoder returned a zero-norm embedding")
            emb=emb/norm
            out["embeddings"][row["id"]]=emb.astype(float).tolist()
        except Exception as e: out["embeddings"][row["id"]]={"error":str(e)}
except Exception as e: out["error"]=str(e)
print(json.dumps(out))
