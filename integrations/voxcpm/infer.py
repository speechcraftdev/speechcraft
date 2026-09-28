#!/usr/bin/env python3
"""One-shot LoRA inference against a VoxCPM checkpoint directory."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VoxCPM LoRA infer")
    parser.add_argument("--checkpoint", required=True, help="LoRA checkpoint dir (or .../latest)")
    parser.add_argument("--text", required=True, help="Transcript to synthesize")
    parser.add_argument("--out", required=True, help="Output WAV path")
    parser.add_argument("--ref", default="", help="Optional reference WAV for cloning")
    parser.add_argument("--ref-text", default="", help="Transcript of --ref")
    parser.add_argument("--base-model", default="", help="Override base model path")
    parser.add_argument("--cfg", type=float, default=None)
    parser.add_argument("--steps", type=int, default=None, dest="inference_timesteps")
    parser.add_argument("--profile", default=str(Path(__file__).resolve().parent / "profile.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = os.environ.get("VOXCPM_REPO", "")
    if repo:
        src = Path(repo) / "src"
        if src.is_dir():
            sys.path.insert(0, str(src))

    import soundfile as sf
    from voxcpm.core import VoxCPM
    from voxcpm.model.voxcpm import LoRAConfig

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    infer = profile.get("infer") or {}
    cfg_value = float(args.cfg if args.cfg is not None else infer.get("cfg_value") or 1.7)
    inference_timesteps = int(
        args.inference_timesteps
        if args.inference_timesteps is not None
        else infer.get("inference_timesteps") or 10
    )

    ckpt = Path(args.checkpoint).expanduser().resolve()
    config_path = ckpt / "lora_config.json"
    if not config_path.is_file():
        raise SystemExit(f"lora_config.json not found in {ckpt}")
    lora_info = json.loads(config_path.read_text(encoding="utf-8"))
    pretrained_path = args.base_model or lora_info.get("base_model") or os.environ.get("VOXCPM_MODEL")
    if not pretrained_path:
        raise SystemExit("base model path missing; pass --base-model or run setup.sh")
    lora_cfg = LoRAConfig(**(lora_info.get("lora_config") or {}))

    if args.ref and not args.ref_text:
        raise SystemExit("--ref requires --ref-text")
    if args.ref_text and not args.ref:
        raise SystemExit("--ref-text requires --ref")

    model = VoxCPM.from_pretrained(
        hf_model_id=str(pretrained_path),
        load_denoiser=False,
        optimize=True,
        lora_config=lora_cfg,
        lora_weights_path=str(ckpt),
    )
    audio = model.generate(
        text=args.text,
        prompt_wav_path=args.ref or None,
        prompt_text=args.ref_text or None,
        cfg_value=cfg_value,
        inference_timesteps=inference_timesteps,
        denoise=False,
    )
    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), audio, model.tts_model.sample_rate)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
