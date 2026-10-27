#!/usr/bin/env python3
"""Deterministic validation loss for F5 checkpoints using official CFM forward."""
import argparse,json,sys
from pathlib import Path
import torch
from datasets import Dataset
from ema_pytorch import EMA
from torch.utils.data import DataLoader
from f5_tts.model import CFM,DiT
from f5_tts.model.dataset import CustomDataset,collate_fn
from f5_tts.model.utils import get_tokenizer
p=argparse.ArgumentParser();p.add_argument('--val-data',required=True);p.add_argument('--vocab',required=True);p.add_argument('--checkpoints',nargs='+',required=True);p.add_argument('--out',required=True);a=p.parse_args()
with (Path(a.val_data)/'duration.json').open() as f:durations=json.load(f)['duration']
raw=Dataset.from_file(str(Path(a.val_data)/'raw.arrow'))
ds=CustomDataset(raw,durations=durations,target_sample_rate=24000,hop_length=256,n_mel_channels=100,n_fft=1024,win_length=1024,mel_spec_type='vocos',preprocessed_mel=False)
vocab,vsize=get_tokenizer(a.vocab,'custom'); mel={'n_fft':1024,'hop_length':256,'win_length':1024,'n_mel_channels':100,'target_sample_rate':24000,'mel_spec_type':'vocos'}
results=[]
for cp in a.checkpoints:
 torch.manual_seed(20261027);torch.cuda.manual_seed_all(20261027)
 model=CFM(transformer=DiT(dim=1024,depth=22,heads=16,ff_mult=2,text_dim=512,conv_layers=4,text_num_embeds=vsize,mel_dim=100),mel_spec_kwargs=mel,vocab_char_map=vocab)
 ema=EMA(model,include_online_model=False)
 d=torch.load(cp,map_location='cpu',weights_only=True);ema.load_state_dict(d['ema_model_state_dict'],strict=True); model=ema.ema_model.to('cuda').eval()
 loader=DataLoader(ds,batch_size=1,shuffle=False,num_workers=0,collate_fn=collate_fn); vals=[]
 with torch.inference_mode():
  for i,b in enumerate(loader):
   torch.manual_seed(20261027+i);torch.cuda.manual_seed_all(20261027+i)
   loss,_,_=model(b['mel'].permute(0,2,1).to('cuda'),text=b['text'],lens=b['mel_lengths'].to('cuda'))
   vals.append(float(loss.detach().cpu()))
 res={'checkpoint':str(Path(cp).resolve()),'update':int(d.get('update',-1)),'validation_count':len(vals),'validation_mean_loss':sum(vals)/len(vals),'validation_losses':vals,'metric':'mean upstream CFM loss on held-out val; fixed RNG seed per clip'}
 results.append(res);print(json.dumps({k:v for k,v in res.items() if k!='validation_losses'}),flush=True)
 del model,ema,d;torch.cuda.empty_cache()
Path(a.out).write_text(json.dumps(results,indent=2)+'\n')
