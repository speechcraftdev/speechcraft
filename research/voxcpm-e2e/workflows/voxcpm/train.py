#!/usr/bin/env python3
"""Invoke the upstream VoxCPM fine-tune function from a frozen YAML config.

The upstream argbind CLI omits its two required no-default parameters from
argument binding. This thin adapter calls that exact upstream function with
resolved config kwargs; the model/trainer implementation is unchanged.
"""
import argparse, importlib.util, random, sys
from pathlib import Path
import numpy as np, torch, yaml
ap=argparse.ArgumentParser();ap.add_argument('config');ap.add_argument('--seed',type=int,required=True);ap.add_argument('--resolved',required=True);ap.add_argument('--upstream-script',default=None);args=ap.parse_args()
cfg=yaml.safe_load(Path(args.config).read_text())
Path(args.resolved).write_text(yaml.safe_dump(cfg,sort_keys=True))
seed=args.seed;random.seed(seed);np.random.seed(seed%(2**32));torch.manual_seed(seed)
if torch.cuda.is_available():torch.cuda.manual_seed_all(seed)
torch.backends.cudnn.deterministic=True;torch.backends.cudnn.benchmark=False
script=Path(args.upstream_script or (Path(__import__('os').environ['VOXCPM_REPO'])/'scripts/train_voxcpm_finetune.py'))
spec=importlib.util.spec_from_file_location('upstream_voxcpm_finetune',script);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
# Ensure this calls the pinned working directory implementation.
m.train(**cfg)
