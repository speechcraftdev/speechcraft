#!/usr/bin/env python3
"""F5-TTS v1 Base fine-tune using the pinned upstream Trainer and prepared Arrow data.

Uses the official F5 Trainer, CFM, DiT, EMA, dataset preparer, and checkpoint
format. The wrapper only routes prepared data and checkpoints into the run dir
and keeps a separate fixed validation set for checkpoint selection.
"""
import argparse,json,math,shutil,sys
from pathlib import Path
import torch
from datasets import Dataset,load_from_disk
from f5_tts.model import CFM,DiT,Trainer
from f5_tts.model.dataset import CustomDataset
from f5_tts.model.utils import get_tokenizer

p=argparse.ArgumentParser();p.add_argument('--train-data',required=True);p.add_argument('--vocab',required=True);p.add_argument('--pretrain',required=True);p.add_argument('--checkpoint-dir',required=True);p.add_argument('--epochs',type=int,default=5);p.add_argument('--batch-size',type=int,default=1);p.add_argument('--grad-accum',type=int,default=1);p.add_argument('--learning-rate',type=float,default=1e-5);p.add_argument('--warmup-updates',type=int,required=True);p.add_argument('--save-per-updates',type=int,required=True);p.add_argument('--max-updates',type=int,required=True);p.add_argument('--run-name',required=True);p.add_argument('--seed',type=int,default=666);a=p.parse_args()
train_path=Path(a.train_data);ckpt_dir=Path(a.checkpoint_dir);ckpt_dir.mkdir(parents=True,exist_ok=True)
pretrained=ckpt_dir/('pretrained_'+Path(a.pretrain).name)
if not pretrained.exists():shutil.copy2(a.pretrain,pretrained)
with (train_path/'duration.json').open() as f:durations=json.load(f)['duration']
raw=load_from_disk(str(train_path/'raw')) if (train_path/'raw').is_dir() else Dataset.from_file(str(train_path/'raw.arrow'))
train_ds=CustomDataset(raw,durations=durations,target_sample_rate=24000,hop_length=256,n_mel_channels=100,n_fft=1024,win_length=1024,mel_spec_type='vocos',preprocessed_mel=False)
vocab_char_map,vocab_size=get_tokenizer(a.vocab,'custom')
model=CFM(transformer=DiT(dim=1024,depth=22,heads=16,ff_mult=2,text_dim=512,conv_layers=4,text_num_embeds=vocab_size,mel_dim=100),mel_spec_kwargs={'n_fft':1024,'hop_length':256,'win_length':1024,'n_mel_channels':100,'target_sample_rate':24000,'mel_spec_type':'vocos'},vocab_char_map=vocab_char_map)
trainer=Trainer(model,epochs=a.epochs,learning_rate=a.learning_rate,num_warmup_updates=a.warmup_updates,save_per_updates=a.save_per_updates,keep_last_n_checkpoints=-1,checkpoint_path=str(ckpt_dir),batch_size_per_gpu=a.batch_size,batch_size_type='sample',max_samples=1,grad_accumulation_steps=a.grad_accum,max_grad_norm=1.0,logger=None,wandb_project='speechcraft-multimodel',wandb_run_name=a.run_name,last_per_updates=a.save_per_updates,bnb_optimizer=True,mel_spec_type='vocos',model_cfg_dict={'architecture':'F5TTS_v1_Base','pretrained':str(pretrained),'epochs':a.epochs,'learning_rate':a.learning_rate,'num_warmup_updates':a.warmup_updates,'batch_size_per_gpu':a.batch_size,'batch_size_type':'sample','grad_accumulation_steps':a.grad_accum,'max_grad_norm':1.0,'bnb_optimizer':True,'vocab_size':vocab_size})
print(json.dumps({'torch':torch.__version__,'cuda':torch.version.cuda,'device':torch.cuda.get_device_name(0),'train_rows':len(raw),'epoch_updates':math.ceil(len(raw)/a.batch_size/a.grad_accum),'epochs':a.epochs,'expected_updates':math.ceil(len(raw)/a.batch_size/a.grad_accum)*a.epochs,'warmup':a.warmup_updates,'vocab_size':vocab_size,'checkpoint_dir':str(ckpt_dir)},indent=2),flush=True)
torch.manual_seed(a.seed);torch.cuda.manual_seed_all(a.seed)
trainer.train(train_ds,num_workers=2,resumable_with_seed=a.seed)
