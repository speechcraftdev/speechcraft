# Experimental workflow adapters

These source-only adapters were used in the research pipeline. VoxCPM model code and base weights remain in an independently installed upstream checkout. F5-TTS is optional and separately installed.

- `voxcpm/train.py` calls the upstream `train_voxcpm_finetune.py` trainer with a resolved config. Set `VOXCPM_REPO` or pass `--upstream-script`. Run `python workflows/voxcpm/train.py --help` for arguments.
- `voxcpm/select_checkpoint.py` selects the minimum logged validation loss among checkpoints saved at validation steps.
- `f5tts/train.py` and `f5tts/score_validation.py` wrap the upstream F5-TTS v1 trainer and validation forward pass. F5 dependencies are optional.
- `generation/generate_condition_group.py` implements restartable condition generation. Set `SPEECHCRAFT_EXPERIMENT_DIR`, `VOXCPM_REPO`, and `VOXCPM_BASE_MODEL`; F5 additionally uses `F5TTS_REPO` and `F5TTS_BASE_CHECKPOINT`. The run directory contains a private `experiment-plan.json`, prompt manifest, reference manifest, training checkpoints, and outputs. `experiment-plan.example.json` documents the expected structure.
- `generation/run_generation_queue.py` reads the same run directory and serially dispatches the planned generation groups. `VOXCPM_PYTHON` and `F5TTS_PYTHON` select their interpreters; both default to the active interpreter.
- `reference_sensitivity/summarize_reference_sensitivity.py` summarizes a local R0–R10 run tree, exports reference and per-prompt tables, and produces dependency-free SVG heatmaps. Set `SPEECHCRAFT_SENSITIVITY_RUN_DIR`.
- `dataset/freeze_shared_prompts.py` screens authored English sentence candidates against user-supplied JSON/JSONL corpora, then freezes a deterministic prompt manifest. See `--help`.
- `../tools/blind-listening/app.py` serves a prepared private listening session; keep media, answer keys, and responses out of version control.

These scripts require local manifests, checkpoints and run directories that are intentionally not included in the handoff. They do not change metric calculations.
