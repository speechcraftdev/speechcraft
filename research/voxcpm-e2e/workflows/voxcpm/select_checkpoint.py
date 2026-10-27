#!/usr/bin/env python3
"""Select an E2E VoxCPM checkpoint using logged validation total only."""
import argparse,hashlib,json,re
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('training_dir',type=Path);a=p.parse_args();root=a.training_dir.resolve();log=root/'checkpoints/train.log'
if not log.is_file():raise SystemExit(f'missing {log}')
vals={int(s):float(v) for s,v in re.findall(r'^\[val\] step (\d+): loss/total: ([0-9.eE+-]+)',log.read_text(),re.M)}
if not vals:raise SystemExit('no validation losses found')
rows=[]
for step,loss in sorted(vals.items()):
 cp=root/'checkpoints'/f'step_{step:07d}'/'lora_weights.safetensors'
 if cp.is_file():rows.append((loss,step,cp))
if not rows:raise SystemExit('no saved checkpoint matches a logged validation step')
loss,step,cp=min(rows)
h=hashlib.sha256(cp.read_bytes()).hexdigest()
out={'selection_rule':'minimum logged validation loss/total among checkpoints saved at that exact validation step','selected_step':step,'validation_loss_total':loss,'checkpoint':str(cp),'lora_weights_sha256':h,'candidate_validation_losses':[{'step':s,'loss_total':v,'checkpoint_saved':any(t==s for _,t,_ in rows)} for s,v in sorted(vals.items())]}
(root/'selected-checkpoint.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
