# VoxCPM blessed path

Speechcraft stops at `speechcraft_dataset.jsonl`. These scripts turn that snapshot
into a VoxCPM1.5 LoRA finetune using a pinned upstream commit.

They wrap OpenBMB/VoxCPM. They do not reimplement training.

```text
setup.sh     pin + venv + VoxCPM1.5 weights
compile.py   canonical JSONL → VoxCPM train.jsonl
train.sh     LoRA finetune (8GB-safe defaults from the existing voice runs)
infer.sh     one transcript → one wav
```

Pinned commit and train knobs live in `profile.json`.

## Usage

```bash
cd integrations/voxcpm
./setup.sh

python3 compile.py \
  --manifest /path/to/canonical_export_... \
  --out /tmp/voice/train.jsonl

./train.sh --manifest /tmp/voice/train.jsonl --out /tmp/voice/ckpts

./infer.sh \
  --checkpoint /tmp/voice/ckpts/latest \
  --text "Hello, this is a test." \
  --out /tmp/voice/hello.wav
```

Optional voice-cloning infer:

```bash
./infer.sh \
  --checkpoint /tmp/voice/ckpts/latest \
  --text "Hello, this is a test." \
  --ref /path/to/ref.wav \
  --ref-text "Transcript of the reference clip." \
  --out /tmp/voice/hello.wav
```

`setup.sh` writes `.vendor/env.sh`. `train.sh` / `infer.sh` source it.

To reuse an already-working Python env instead of the vendor venv:

```bash
export VOXCPM_PYTHON=/path/to/python
export VOXCPM_REPO=/path/to/VoxCPM/checkout-at-the-pin
export VOXCPM_MODEL=/path/to/openbmb__VoxCPM1.5
```

The clone SHA must still match `profile.json`. Do not point this at a dirty checkout.

## What this is not

- Not an in-app Training page
- Not a generic multi-model adapter registry
- Not a copy of the VoxCPM training loop
