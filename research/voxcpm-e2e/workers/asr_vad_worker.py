"""Runs in a pre-existing compatible faster-whisper/Silero environment."""
import json, sys
payload=json.load(sys.stdin)
out={"transcripts":{},"vad":{},"versions":{}}
try:
    import faster_whisper
    from faster_whisper import WhisperModel
    out["versions"]["faster-whisper"]=getattr(faster_whisper,"__version__","unknown")
    model=WhisperModel(payload.get("model","large-v3"),device=payload.get("device","cpu"),compute_type=payload.get("compute_type","int8"),download_root=payload.get("model_cache"))
    for row in payload.get("transcribe",[]):
        try:
            segs,info=model.transcribe(row["path"],language=payload.get("language"),vad_filter=False,beam_size=payload.get("beam_size",5))
            out["transcripts"][row["id"]]={"text":" ".join(s.text.strip() for s in segs).strip(),"language":info.language,"duration":info.duration}
        except Exception as e: out["transcripts"][row["id"]]={"error":str(e)}
except Exception as e: out["asr_error"]=str(e)
if payload.get("vad"):
  try:
    import numpy as np, soundfile as sf
    import torch
    from silero_vad import load_silero_vad, get_speech_timestamps
    from importlib.metadata import version
    out["versions"]["silero-vad"]=version("silero-vad")
    out["versions"]["torch"]=torch.__version__
    out["versions"]["cuda_available"]=torch.cuda.is_available()
    out["versions"]["cuda_build"]=torch.version.cuda
    try: out["versions"]["torchaudio"]=__import__("torchaudio").__version__
    except Exception: pass
    vad=load_silero_vad()
    for row in payload.get("vad",[]):
      try:
        x,sr=sf.read(row["path"],dtype="float32",always_2d=True); x=x.mean(axis=1)
        if sr!=16000:
          import scipy.signal
          from math import gcd
          g=gcd(sr,16000); x=scipy.signal.resample_poly(x,16000//g,sr//g).astype("float32")
        ts=get_speech_timestamps(torch.from_numpy(np.asarray(x,dtype="float32")),vad,sampling_rate=16000)
        out["vad"][row["id"]]={"speech_segments":len(ts),"speech_seconds":sum(z["end"]-z["start"] for z in ts)/16000,"duration_seconds":len(x)/16000}
        out["vad"][row["id"]]["silence_fraction"]=max(0.0,1-out["vad"][row["id"]]["speech_seconds"]/max(out["vad"][row["id"]]["duration_seconds"],1e-9))
      except Exception as e: out["vad"][row["id"]]={"error":str(e)}
  except Exception as e: out["vad_error"]=str(e)
print(json.dumps(out))
