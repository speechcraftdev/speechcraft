# SpeechCraft Dataset Worker

This worker owns the heavy audio dataset pipeline runtime. The FastAPI backend
starts it as a subprocess and reads `status.json`, logs, summaries, and artifact
files from the run root. The backend must not import this package.

Runtime boundary:

- `backend/`: light API, database, run orchestration, artifact indexing.
- `workers/dataset/`: CUDA/audio stack for VAD, NeMo diarization, locked VR
  slicing, candidate assembly, transcript QC, speaker purity, and native-rate
  export.
- Production slicing does not invoke MFA, Whisper, or word-alignment QC.

Initial check:

```bash
cd workers/dataset
uv sync
uv run python scripts/preflight.py --json --artifact-root /tmp/speechcraft-dataset-preflight
```

The script prints a single JSON object suitable for backend/UI consumption.

ASR model setup/check:

```bash
cd workers/dataset
uv run python -m speechcraft_dataset.models download-asr --model tiny.en --json
uv run python -m speechcraft_dataset.models check-asr --model tiny.en --load-model --json
```

For fully local/offline runs, pass a converted faster-whisper model directory in
the worker config:

```json
{
  "faster_whisper_model_path": "/absolute/path/to/local/faster-whisper-model"
}
```

faster-whisper remains a later Transcript Correctness dependency, not a slicer
dependency. Preflight reports local ASR model availability for stages that
actually transcribe.

First CLI path:

```bash
cd workers/dataset
uv run python -m speechcraft_dataset.run \
  --run-root /tmp/speechcraft-dataset-run \
  --source-wav /path/to/source.wav \
  --single-speaker
```

Repeat `--source-wav` for multi-WAV datasets.

Current production slicing is the locked VR `O0_4` path: source preparation,
mono 16 kHz analysis audio, Silero timestamp VAD for diarization regions,
trusted-region packing buffers, then Silero ONNX frames + percentile RMS +
duration-only packing.

```text
source_audio -> audio_variants -> vad -> diarization -> buffers -> candidate_review_clips -> transcript_qc -> speaker_purity -> native_export
```

Default `--stop-after` is `candidate_review_clips`. Whisper Large-v3 is used
later for Transcript Correctness, not to create slices.

Use `--stop-after audio_variants` when checking the run-root/audio contract
without the heavy Silero/PyTorch worker environment.

Use `--stop-after buffers` to write trusted-region packing scopes after VAD
and speaker selection. These are not ASR/MFA chunks and do not emit buffer WAVs.

Use `--stop-after candidate_review_clips` to run the locked VR slicer and write
3-15 second review WAVs targeting 8 seconds, plus `vr_cutpoints.jsonl`. These
are review artifacts, not final training exports. `training_text` is empty until
TC transcribes the clip.

Use `--stop-after native_export` to cut the candidate clips from the original
source WAV sample rate using the analysis-to-native sample mapping. Native
exports write `export_manifest.json`, `export_audit.json`, `export_summary.json`,
and `native_export_clips/*.wav`.

NeMo diarization, speaker-purity QC, dataset QC, review-decision persistence,
and VoxCPM manifest export remain after this stage contract is stable.

## Transcript confidence QC (Whisper B1-LJ)

Production transcript-confidence scoring runs on **final candidate review clips**
with clip-level `faster-whisper` (`large-v3` by default) and the frozen B1-LJ
formula. The old Wav2Vec2 CTC / greedy-decode TC path has been removed.

```bash
cd workers/dataset
uv run python -m speechcraft_dataset.analyze_whisper_b1_transcript_qc \
  --run-root /path/to/dataset-run \
  --model large-v3 \
  --device auto
```

Behavior notes:

- Whisper is loaded once per analysis run and reused across clips.
- Required transcription options: `word_timestamps=True`, `vad_filter=False`,
  `condition_on_previous_text=False`.
- Scoring uses lexical word probabilities only; punctuation-only tokens are excluded.
- Zero lexical words leave `transcript_match_score` as `null`, mark the clip
  review-required, and emit `no_lexical_words`.
- Segments with lexical content and `no_speech_prob >= 0.7` are review-required
  without mutating the B1-LJ formula.
- Number/symbol hazards are review metadata, not score penalties.
- Config keys such as `transcript_qc_backend=ctc` or Wav2Vec2 model ids fail with
  an actionable removal error.
- TC scoring is independent of VR slicing; it does not score packing buffers.

## Backend Run Lifecycle

The FastAPI backend now owns the coarse `ProcessingRun` lifecycle without
importing this package:

```text
POST /api/projects/{project_id}/dataset-runs
POST /api/dataset-runs/{run_id}/start
POST /api/dataset-runs/{run_id}/refresh
GET  /api/dataset-runs/{run_id}
GET  /api/dataset-runs/{run_id}/log
```

Runs live under the backend storage root at
`dataset-runs/{project_id}/{run_id}`. Refreshing a run reads `status.json`,
indexes known file-backed artifacts with hashes, and fails closed if the worker
exits without writing status.
