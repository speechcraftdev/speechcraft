# TitaNet backend comparison (evaluation only)

This directory holds a reproducible runner for comparing the existing SpeechCraft NeMo SP math with a sherpa-onnx FP32 embedding provider. Production code and dependency locks are not changed.

## Archived corpus

Default input is `/home/aaravthegreat/Projects/speaker_ts_eval/runs/2026-09-09_pyannote_sp_incremental`:

* `clips.jsonl` and `clip_wavs/<source>/<clip>.wav`: exact pyannote-derived 798-clip population, clip IDs, leak seconds and labels.
* `sources/<source>/source.json`: frozen enrollment summaries and target speaker IDs.
* `sources/<source>/enrollment_chunks.jsonl` and `enrollment/all_chunks/*.wav`: exact 20 ordered three-second chunks per source (480 total), including old gold wrong-speaker diagnostics.
* Experiment origin and frozen settings are in `/home/aaravthegreat/Projects/speaker_ts_eval/scripts/run_pyannote_sp_incremental.py`, `tests/diarization_bakeoff/evaluate_pipeline.py`, `sp_incremental.py`, and `tuning/configs.py`.

The archive is read-only input and is intentionally not copied or committed. Every WAV is decoded as mono float32 at 16 kHz. Its SHA256 over the float32 sample bytes is recorded before embedding.

## Environments and invocation

Use the existing worker environment only for the NeMo process. Create a separate evaluation venv for sherpa-onnx; do not install sherpa into the worker environment. Install the pinned packages from `requirements-sherpa.txt` there. Obtain the ONNX file explicitly, pin it to a reviewed revision, and pass a local file path. The runner never downloads weights.

Run the same command once per backend, in its respective environment:

```sh
workers/dataset/.venv/bin/python experiments/titanet_onnx_parity/run.py --backend nemo --corpus /path/to/2026-09-09_pyannote_sp_incremental --out runs/titanet_onnx_parity/nemo
experiments/titanet_onnx_parity/.venv/bin/python experiments/titanet_onnx_parity/run.py --backend sherpa --corpus /path/to/2026-09-09_pyannote_sp_incremental --model /path/to/nemo_en_titanet_large.onnx --out runs/titanet_onnx_parity/sherpa
workers/dataset/.venv/bin/python experiments/titanet_onnx_parity/rescore.py --corpus /path/to/2026-09-09_pyannote_sp_incremental --run runs/titanet_onnx_parity/nemo
workers/dataset/.venv/bin/python experiments/titanet_onnx_parity/rescore.py --corpus /path/to/2026-09-09_pyannote_sp_incremental --run runs/titanet_onnx_parity/sherpa
python experiments/titanet_onnx_parity/summarize.py --nemo runs/titanet_onnx_parity/nemo --sherpa runs/titanet_onnx_parity/sherpa --out runs/titanet_onnx_parity
```

Outputs in `runs/titanet_onnx_parity/` are ignored and include embedding interchange arrays, diagnostics, paired clip scores, and summary files. The scorer runs under the same worker environment for both arrays and uses `eval_speaker_purity.py` helpers. The summary refuses differing clip IDs, labels, audio item IDs, or float32 audio hashes and records failures rather than converting them to clean scores. CPU is forced for both backends.

## Focused checks and edge probes

```sh
experiments/titanet_onnx_parity/.venv/bin/python -m py_compile experiments/titanet_onnx_parity/*.py
experiments/titanet_onnx_parity/.venv/bin/python experiments/titanet_onnx_parity/test_parity.py
workers/dataset/.venv/bin/python experiments/titanet_onnx_parity/stability.py --backend nemo --audio /path/to/enroll_000.wav --out runs/titanet_onnx_parity/nemo/stability.json
experiments/titanet_onnx_parity/.venv/bin/python experiments/titanet_onnx_parity/stability.py --backend sherpa --audio /path/to/enroll_000.wav --model /path/to/nemo_en_titanet_large.onnx --out runs/titanet_onnx_parity/sherpa/stability.json
workers/dataset/.venv/bin/python experiments/titanet_onnx_parity/edge_diagnostics.py --backend nemo --corpus /path/to/2026-09-09_pyannote_sp_incremental --out runs/titanet_onnx_parity/nemo
experiments/titanet_onnx_parity/.venv/bin/python experiments/titanet_onnx_parity/edge_diagnostics.py --backend sherpa --corpus /path/to/2026-09-09_pyannote_sp_incremental --model /path/to/nemo_en_titanet_large.onnx --out runs/titanet_onnx_parity/sherpa
```

The edge probes use gold only to select clean-target and known-wrong-speaker examples, after the corpus run; no gold field is passed to an embedding provider or scorer. The archive has no independent noise-condition labels, so the harness does not claim a curated noisy-speech subset. The deterministic probes include clean and known-wrong enrollment speech, a high-leak clip, shortest/longest clips, synthetic silence, and mostly silence. On completion, `summarize.py` writes `comparison.json`, `comparison.csv`, `per_clip_scores.csv`, `embedding_diagnostics.csv`, `enrollment_diagnostics.csv`, `runtime_benchmark.json`, and `report.md` in the ignored run directory. The compact reviewed report is copied to `experiments/titanet_onnx_parity/report.md` for source control.

## Provenance caution

The current NeMo loader uses `nvidia/speakerverification_en_titanet_large`. Its locally cached checkpoint is recorded in `model_metadata.json` when run. The ONNX repository is `csukuangfj/speaker-embedding-models`, file `nemo_en_titanet_large.onnx`; metadata pins its SHA256. Matching layer tensors and model metadata strengthen the identity evidence, but the upstream repository currently has no model card tying its export to a specific NeMo checkpoint revision. Treat this as a replacement-model comparison unless the full tensor comparison in the run artifacts closes that provenance gap.
