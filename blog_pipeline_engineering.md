# How Speechcraft Turns Raw Audio Into Training-Ready Clips

This post is for engineers who want to understand the real pipeline — what happens at every stage, what tools are used, why specific design decisions were made, and where the tradeoffs live. If you need to debug a failed run, tune a parameter, or extend the pipeline, this is your map.

---

## The Problem

You have hours of raw audio — interviews, audiobooks, game dialogue, YouTube recordings. You want short, clean, single-speaker clips with accurate transcripts, ready to train a text-to-speech model.

The gap between "raw WAV file" and "training-ready clip" is enormous:

- The audio might be stereo at 44.1 kHz. Your models want mono 16 kHz.
- You need to know *where* someone is speaking and *where* they're silent.
- If there are multiple speakers, you need to know *who* is speaking *when* — and only keep your target.
- You need a transcript, but you don't have one.
- That transcript needs to be time-aligned to the audio at the word level.
- You need to find safe places to cut the audio into 3–15 second clips without slicing through a word or catching a breath.
- Every clip needs a verified transcript that exactly matches what was spoken.

Speechcraft does all of this automatically, in a single sequential pipeline.

---

## Architecture Overview

The pipeline has two halves that never import each other:

- **Backend** (FastAPI) — handles the UI, stores metadata in SQLite, and spawns the worker as a subprocess.
- **Worker** (`speechcraft_dataset.run`) — does all the heavy lifting. Runs in its own Python environment with CUDA, PyTorch, and the full audio stack.

Communication is file-based. The worker writes JSON artifacts and a `status.json` heartbeat. The backend polls the filesystem. No message queues, no RPC, no shared memory.

A dataset run produces a directory tree:

```
dataset-runs/{project_id}/{run_id}/
├── config.json
├── status.json
├── audio/analysis/          # mono 16 kHz variants
├── artifacts/
│   ├── buffers/             # chunked WAVs for ASR
│   ├── asr_mfa_queue/       # filtered buffers
│   ├── mfa_corpus/          # text + audio for MFA
│   ├── mfa_output/          # TextGrid alignments
│   ├── candidate_review_clips/
│   └── native_export_clips/
└── logs/
```

The pipeline is **16 sequential stages**. Each stage reads from previous artifacts and writes its own. If any stage fails, the run stops and the error is recorded in `status.json` with typed reason codes.

---

## Stage 1: Source Audio Inspection

**What happens:** The worker validates every source WAV — checks it's actually PCM, reads the sample rate, channel count, duration, and computes a SHA-256 content hash.

**Why it matters:** Everything downstream depends on knowing the native sample rate (for final export) and having a content hash (for provenance tracking). If someone swaps the source file after processing, the hash mismatch is detectable.

**Output:** `source_audio_manifest.json` — one entry per source file with all metadata.

---

## Stage 2: Analysis Audio Conversion

**What happens:** Every source WAV is converted to **mono 16 kHz PCM** using ffmpeg:

```
ffmpeg -y -i SOURCE -ac 1 -ar 16000 -c:a pcm_s16le OUTPUT
```

**Why mono 16 kHz:** Every model in the pipeline (Silero VAD, NeMo diarization, Whisper ASR, MFA, Wav2Vec2 QC) expects mono 16 kHz input. Converting once at the start means every downstream stage reads from the same standardized audio.

**Why not normalize loudness:** Deliberate choice. Loudness normalization before VAD and ASR can mask genuine volume issues in the source (mic level changes, room noise). The pipeline preserves the original dynamics and lets downstream stages deal with what they get.

**Output:** `audio/analysis/{source_id}.mono16000.wav` per source file.

---

## Stage 3: Voice Activity Detection (VAD)

**What happens:** Silero VAD runs over each analysis WAV and marks every region where someone is speaking.

**Tool:** [Silero VAD](https://github.com/snakers4/silero-vad) — a small, fast neural VAD. No GPU required.

**How it works:** The model processes audio in chunks and outputs per-frame speech probabilities. `get_speech_timestamps()` converts these into discrete speech segments using configurable thresholds:

| Parameter | Default | What it controls |
|-----------|---------|-----------------|
| `vad_threshold` | 0.5 | Probability above which a frame is "speech" |
| `vad_min_speech_ms` | 250 | Shortest speech segment to keep |
| `vad_min_silence_ms` | 250 | Shortest silence gap to treat as a real pause |
| `vad_speech_pad_ms` | 80 | Padding added to each side of a speech segment |

**Output:** `vad_segments.jsonl` — one line per speech segment with sample-level start/end positions and timestamps.

**Tradeoff:** Lower `vad_threshold` catches quieter speech but also catches more noise and breath sounds. The defaults are tuned for typical close-mic recordings. Noisy or distant-mic audio may need a higher threshold.

---

## Stage 4: Diarization

**What happens:** The pipeline figures out *who* is speaking in each speech segment. There are two modes.

### Single-speaker mode (default)

If you tell the pipeline there's only one speaker, diarization is trivially solved: every VAD segment gets labeled `speaker_0`. No neural model runs. The pipeline writes a `speaker_selection.json` with the target speaker auto-selected and moves on.

This is the common case for character voice datasets where you've already isolated the audio.

### Multi-speaker mode

For interviews, podcasts, or multi-character recordings, the pipeline runs **NeMo ClusteringDiarizer** with TitaNet speaker embeddings.

**Tool:** [NVIDIA NeMo](https://github.com/NVIDIA/NeMo) — specifically `ClusteringDiarizer` with `titanet_large` for speaker embeddings.

**Key design: windowed diarization.** NeMo's diarizer doesn't scale well to multi-hour files. So the pipeline splits the audio into overlapping windows (default 900 seconds with 30 seconds of overlap) and runs diarization independently on each window.

The tricky part is **stitching speaker labels across windows.** Speaker "A" in window 1 and speaker "A" in window 2 aren't guaranteed to be the same person — they're independent clustering results. The pipeline handles this by:

1. Extracting the 30-second overlap region from both windows
2. Comparing which speakers from each window are active during the overlap
3. Mapping labels based on temporal co-occurrence (requires at least 1 second of overlap)
4. Discarding the first half of each overlap region to avoid double-counting

**Important:** The pipeline uses **Silero VAD segments as external VAD input** to NeMo, not NeMo's built-in VAD. This means the same VAD decisions from Stage 3 feed into diarization — no conflicting speech boundaries.

**After diarization:** The pipeline pauses. It writes `speaker_selection.json` with `"selected": false` and exits. The user picks their target speaker in the UI (listening to preview clips), then resumes processing. The backend relaunches the worker with the selected `target_speaker_id`.

**Output:** `speaker_regions.jsonl` — timed regions per speaker. `speaker_samples_manifest.json` — up to 3 preview clips per speaker (6 seconds each) for the selection UI.

---

## Stage 5: Processing Buffers

**What happens:** The pipeline takes the target speaker's regions and splits them into chunks suitable for ASR — roughly 24 seconds each, with padding, never exceeding 29.5 seconds.

**Why not just send the whole file to Whisper:** Two reasons. First, Whisper's attention mechanism degrades on long inputs — you get hallucinated repeats, missed segments, and degraded timestamp accuracy. Second, MFA (Montreal Forced Aligner) has similar scaling issues. Chunking to ~24 seconds keeps both tools in their reliable operating range.

**The splitting algorithm:**

1. **Merge** adjacent target-speaker regions — unless a different speaker talks in the gap between them. If speaker B says something between two speaker A regions, that's a hard boundary. If there's just silence, merge them.

2. **Split** long merged regions at natural pauses. The pipeline searches for a VAD silence gap near the 24-second mark (within ±2s initially, widening to ±4s if needed). Among candidate gaps, it picks the **quietest point** — the sample with the lowest RMS energy, found using a 20ms-frame / 10ms-hop energy analysis with 3-frame smoothing.

3. **Pad** each chunk by 0.25–0.5 seconds on each side. This padding gives ASR context at chunk boundaries and prevents MFA from struggling with words that start right at the edge.

4. **Enforce** the 29.5-second hard limit. If a chunk can't be split naturally, the pipeline forces a split at 15 seconds and flags it as `provisional_pre_mfa_split`.

**Output:** `processing_buffers.json` — metadata for each chunk. `artifacts/buffers/buffer_NNNNNN.wav` — the actual audio files.

---

## Stage 6: ASR Queue

**What happens:** A simple filter. Buffers shorter than a minimum duration (default 1.0s from backend, 5.0s from worker defaults) are rejected — they're too short for reliable ASR. Surviving buffers are copied to `artifacts/asr_mfa_queue/` and listed in `asr_mfa_queue.json`.

Rejected buffers go to `rejected_buffers.json` for auditability.

---

## Stage 7: ASR (Automatic Speech Recognition)

**What happens:** Every queued buffer gets transcribed by Whisper.

**Tool:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — a CTranslate2 reimplementation of OpenAI's Whisper. Runs ~4x faster than the original with the same accuracy.

**Model:** Configurable. Backend defaults to `large-v3` (best accuracy), worker defaults to `small.en` (fast for development). Resolved from HuggingFace cache or a local path.

**The transcription call:**

```python
model.transcribe(
    audio_path,
    language=language,
    vad_filter=False,
    word_timestamps=False,
    condition_on_previous_text=False,
    beam_size=5,
)
```

**Three deliberate decisions here:**

1. **`vad_filter=False`** — Whisper has its own internal VAD. We disable it because we already did VAD in Stage 3 with Silero, and the buffer boundaries are already clipped to speech regions. Running Whisper's VAD on top would be redundant and could cause it to skip content near chunk edges.

2. **`word_timestamps=False`** — Whisper's word-level timestamps use a cross-attention heuristic that's noisy and inconsistent. We don't use them. We get precise word timing from MFA in Stage 9 instead. Whisper gives us the text; MFA gives us the alignment.

3. **`condition_on_previous_text=False`** — Normally Whisper uses its own previous output as context for the next chunk. This helps coherence but causes a dangerous failure mode: if Whisper hallucinates in one chunk, that hallucination contaminates every subsequent chunk. For a training data pipeline where accuracy matters more than fluency, we disable it.

**Output:** `transcripts.json` — per-buffer transcript with segment-level confidence scores, detected language, and the full text.

---

## Stage 8: Transcript Normalization

**What happens:** The raw Whisper transcript gets cleaned up for MFA consumption.

MFA works from a pronunciation dictionary. Words with special characters (`$`, `%`, `@`, `#`, etc.) or raw numbers won't be in any dictionary. The normalizer:

1. Classifies every token as normal text, a symbol hazard, or a numeric hazard
2. Strips hazardous tokens from the alignment text (they'd cause MFA failures)
3. Flags buffers that need human review if significant content was stripped

The normalizer **does not** expand numbers to words (e.g., "42" → "forty-two"). That's a deliberate scope limitation — number expansion is error-prone and language-dependent. Buffers with numbers get flagged for review instead.

**Output:** `normalized_transcripts.json` — per-buffer with both `text` (original) and `alignment_text` (cleaned for MFA). `transcript_hazards.json` — detailed per-token classification.

---

## Stage 9: MFA (Montreal Forced Aligner)

**What happens:** MFA takes the normalized text and the audio and produces word-level time alignments — the exact start and end time of every word in every buffer.

**Tool:** [Montreal Forced Aligner](https://montreal-forced-aligner.readthedocs.io/) — an external binary invoked via subprocess. It uses Kaldi under the hood for HMM-based forced alignment.

**The process:**

1. **Corpus build:** For each buffer with non-empty alignment text, the pipeline writes:
   - `mfa_corpus/{buffer_id}.wav` — the audio
   - `mfa_corpus/{buffer_id}.lab` — the normalized text (one line)

2. **MFA execution:**
   ```
   mfa align --clean --overwrite [--single_speaker] \
       mfa_corpus/ english_us_mfa english_mfa mfa_output/
   ```
   - `english_us_mfa` — pronunciation dictionary
   - `english_mfa` — pre-trained acoustic model
   - `--single_speaker` — tells MFA to skip internal speaker adaptation (faster)

3. **TextGrid parsing:** MFA writes Praat TextGrid files. The pipeline parses these with `praatio`, extracting word-level intervals (start time, end time, word label).

4. **Coordinate mapping:** MFA's timestamps are relative to the buffer audio. The pipeline maps them back to global source-audio sample positions using the buffer's known offset. It also cross-checks that MFA's word sequence matches the normalized transcript and flags any OOV (out-of-vocabulary) words that MFA couldn't align.

**Why MFA instead of Whisper's word timestamps:** MFA uses a full HMM alignment with a pronunciation dictionary. It knows that "through" is pronounced /θ ɹ u/ and aligns at the phoneme level before mapping back to word boundaries. Whisper's word timestamps are derived from cross-attention weights — a heuristic that works okay on average but can be off by hundreds of milliseconds on individual words. For training data, those errors compound: a 200ms misalignment means you're teaching the TTS model that a word starts 200ms before the speaker actually says it.

**Output:** `aligned_words.jsonl` — per-word entries with source-level sample positions, buffer ID, word text, and hazard/OOV flags.

---

## Stage 10: Alignment QC

**What happens:** A sanity check on MFA's output before we try to cut clips.

Per buffer, the pipeline checks:
- Are there any aligned words at all?
- Does the word count match the normalized transcript?
- Are words in chronological order?
- Are all words within the buffer's trusted boundaries?
- Are any words suspiciously short (< 20ms) or long (> 2s)?

Buffers with fatal issues get `disable_automatic_cutpoints: true` — the pipeline won't try to auto-slice them. Buffers with warnings (words near the trusted edge, OOV words) still proceed but carry flags.

**Output:** `alignment_qc_by_buffer.json` — per-buffer QC results with reason codes.

---

## Stage 11: SafeCutPoints

**What happens:** The pipeline finds safe places to cut the audio into clips. A "safe" cut point is a quiet moment between two words where you can slice without cutting through speech or catching artifacts.

**The algorithm for each inter-word gap:**

1. **Apply word-edge guards.** Skip the first 30ms after the previous word ends and the last 30ms before the next word starts. This prevents cutting into the tail of a plosive or the onset of a fricative.

2. **Check gap width.** After guards, the usable gap must be at least 80ms. Shorter gaps are too tight to cut cleanly.

3. **Find the quietest point.** Within the usable gap, compute RMS energy in 10ms frames with 5ms hops. Pick the frame with the lowest energy — that's your cut point.

4. **Noise floor check.** Compute the buffer's noise floor (10th percentile of all RMS frames). Reject the cut point if its valley is more than 6 dB above the noise floor. A "quiet" gap that's still louder than ambient probably has a breath, room noise bleed, or trailing speech.

5. **Hazard exclusion zones.** No cut point within 0.5 seconds of an OOV word, symbol token, numeric token, or provisional buffer split boundary. These zones protect against cutting next to words whose alignment might be unreliable.

**Output:** `safe_cutpoints.jsonl` (accepted) and `rejected_cutpoint_candidates.jsonl` (rejected with reasons).

---

## Stage 12: Candidate Clip Assembly

**What happens:** The pipeline walks the safe cut points and greedily assembles clips.

**The greedy algorithm:**

1. For each buffer that has at least 2 safe cut points and passed alignment QC:
2. Start at the first cut point.
3. Walk forward through cut points, looking for one that gives a clip closest to the **target duration** (default 8 seconds), within the **min/max range** (3–15 seconds).
4. Slice the audio between the start and end cut points.
5. Collect all fully-contained aligned words — these become the clip's `training_text`.
6. Advance the start to the end cut point and repeat.

**Why target 8 seconds:** TTS models train best on clips in the 3–15 second range. Too short and there's not enough context for prosody. Too long and attention mechanisms struggle. 8 seconds is a sweet spot that balances context with training efficiency.

**Output:** `candidate_review_clips/*.wav` — the actual clip audio files (16 kHz analysis rate). `candidate_review_manifest.json` — per-clip metadata including training text, word IDs, duration, source buffer, hazard review flags.

---

## Stage 15: Native Export

**What happens:** For clips that pass review, the pipeline cuts the **original source WAV** at its native sample rate — no resampling, no re-encoding.

All previous stages work on the 16 kHz analysis audio. But TTS training benefits from higher sample rates (22.05 kHz, 44.1 kHz, 48 kHz). The native export maps the analysis-rate sample boundaries back to the original:

```
native_sample = analysis_sample × (source_rate / analysis_rate)
```

The slice is done with Python's `wave` module — raw PCM read/write, no ffmpeg, no lossy codec. The original audio quality is preserved exactly.

**Output:** `native_export_clips/{clip_id}.wav` — training-ready clips at the source's original sample rate.

---

## The Full Picture

```
Source WAV (stereo, 44.1 kHz, hours long)
    │
    ▼
[1] Inspect → validate, hash, metadata
    │
    ▼
[2] Convert → mono 16 kHz (ffmpeg)
    │
    ▼
[3] VAD → speech segments (Silero)
    │
    ▼
[4] Diarization → who speaks when (NeMo TitaNet / passthrough)
    │  └─ pause for speaker selection if multi-speaker
    ▼
[5] Buffer → split into ~24s padded chunks at quiet points
    │
    ▼
[6] Queue → filter out short buffers
    │
    ▼
[7] ASR → transcribe each buffer (faster-whisper)
    │
    ▼
[8] Normalize → clean transcript for MFA
    │
    ▼
[9] MFA → word-level time alignment (Montreal Forced Aligner)
    │
    ▼
[10] Alignment QC → sanity check word timings
    │
    ▼
[11] SafeCutPoints → find quiet inter-word gaps
    │
    ▼
[12] Assemble → greedy clip construction (3–15s, target 8s)
    │
    ▼
[15] Native Export → cut original WAV at source sample rate
    │
    ▼
Training-ready clips with aligned transcripts
```

---

## Key Design Decisions

### Why sequential, not parallel?

The stages have strict data dependencies — you can't run ASR before VAD, can't find cut points before MFA alignment. And each stage's memory footprint is large (Whisper, NeMo, Wav2Vec2 can each need several GB of VRAM). Running them concurrently would require multi-GPU setups for marginal benefit.

The pipeline is designed to be re-runnable. If you change slicer parameters, you don't re-run ASR and MFA — there's a dedicated slicer rerun path that picks up from `safe_cutpoints` onward.

### Why file-based communication?

Every intermediate result is inspectable. If MFA fails on buffer 47, you can read `aligned_words.jsonl`, look at `mfa_corpus/buffer_000047.lab`, and understand exactly what went wrong. If the backend crashes mid-run, the worker's artifacts are still on disk — refresh picks them up.

Every stage writes content hashes of its inputs and outputs. You can verify after the fact that no artifact was modified between stages.

### Why Silero for VAD instead of Whisper's built-in?

Silero is a dedicated, lightweight VAD model that runs on CPU in milliseconds. It produces clean speech boundaries that feed into both diarization and buffer construction. Whisper's VAD is an internal heuristic optimized for its own transcription pipeline — not for providing boundaries to other tools.

### Why MFA instead of Whisper word timestamps?

Precision. MFA does phoneme-level HMM alignment using a pronunciation dictionary. It knows that "thought" has 3 phonemes and "strengths" has 7. Whisper estimates word boundaries from attention patterns — a useful approximation, but not precise enough for cutting clips at word boundaries without audible artifacts.

### Why cut at RMS valleys, not at VAD boundaries?

VAD tells you where speech starts and stops at a coarse level. But between two words in the same speech segment, there are micro-pauses — 80–200ms of near-silence. Those are the safe cut points. VAD boundaries are typically at the edges of longer pauses (250ms+), which would limit you to fewer, longer clips. RMS valley detection within inter-word gaps gives you many more candidate cut points while ensuring each one is acoustically clean.

---

## Configuration Quick Reference

The pipeline is configured via `config.json` at the run root. Backend defaults can differ from worker defaults — the backend generally uses tighter timeouts and the larger Whisper model.

| Stage | Key parameters | Typical values |
|-------|---------------|----------------|
| Audio | `analysis_sample_rate` | 16000 |
| VAD | `vad_threshold`, `vad_min_speech_ms` | 0.5, 250 |
| Diarization | `diarization_max_speakers`, `diarization_speaker_model` | 6, `titanet_large` |
| Buffers | `max_processing_buffer_sec`, `target_processing_chunk_sec` | 29.5, 24–25 |
| ASR | `faster_whisper_model`, `asr_language` | `large-v3`, `en` |
| MFA | `mfa_dictionary`, `mfa_acoustic_model`, `mfa_timeout_sec` | `english_us_mfa`, `english_mfa`, 600 |
| Cutpoints | `cutpoint_min_gap_ms`, `cutpoint_noise_margin_db` | 80, 6.0 |
| Clips | `candidate_target_clip_sec`, `candidate_min/max_clip_sec` | 8.0, 3.0/15.0 |

---

## What This Doesn't Cover

This post covers the automated pipeline — source audio in, training clips out. Speechcraft also has:

- **Transcript Match scoring** (clip-level Whisper B1-LJ confidence scoring per candidate clip)
- **Speaker Purity scoring** (embedding similarity to the target speaker)
- **Clip Lab** (per-clip transcript editing and boundary adjustment)
- **QC Page** (threshold-based review with accept/reject workflows)

Those are post-pipeline quality layers. The pipeline described here is the foundation they all operate on.
