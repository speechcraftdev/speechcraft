#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
import wave
from pathlib import Path
from typing import Any


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def probe_wav(path: Path) -> dict[str, Any]:
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        sample_rate = reader.getframerate()
        return {
            "duration_sec": frames / sample_rate if sample_rate else 0.0,
            "sample_rate": sample_rate,
            "channels": reader.getnchannels(),
            "frames": frames,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clips-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--model", default="/home/aaravthegreat/.cache/huggingface/hub/models--Systran--faster-whisper-large-v3/snapshots/edaa852ec7e145841d8ffdb056a99866b5f0a478")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--language", default="en")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--condition-on-previous-text", action="store_true")
    parser.add_argument("--vad-filter", action="store_true")
    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    out_dir = Path(args.out_dir)
    clip_paths = sorted(clips_dir.glob("*.wav"))
    if not clip_paths:
        raise ValueError(f"no wav clips found in {clips_dir}")

    import faster_whisper
    import torch
    from faster_whisper import WhisperModel

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    load_started = time.perf_counter()
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    load_elapsed = time.perf_counter() - load_started

    clip_rows: list[dict[str, Any]] = []
    segment_rows: list[dict[str, Any]] = []
    word_rows: list[dict[str, Any]] = []
    transcript_jsonl_rows: list[dict[str, Any]] = []

    try:
        for clip_index, clip_path in enumerate(clip_paths):
            clip_id = clip_path.stem
            wav_info = probe_wav(clip_path)
            clip_started = time.perf_counter()
            segments_iter, info = model.transcribe(
                str(clip_path),
                language=args.language if args.language != "auto" else None,
                task="transcribe",
                beam_size=args.beam_size,
                word_timestamps=True,
                vad_filter=args.vad_filter,
                condition_on_previous_text=args.condition_on_previous_text,
            )
            segments = list(segments_iter)
            clip_elapsed = time.perf_counter() - clip_started
            text_parts: list[str] = []
            clip_word_count = 0
            for segment_index, segment in enumerate(segments):
                segment_id = f"{clip_id}_segment_{segment_index:04d}"
                segment_text = str(segment.text or "").strip()
                if segment_text:
                    text_parts.append(segment_text)
                segment_rows.append(
                    {
                        "clip_id": clip_id,
                        "segment_id": segment_id,
                        "segment_index": segment_index,
                        "start_sec": segment.start,
                        "end_sec": segment.end,
                        "text": segment.text,
                        "avg_logprob": segment.avg_logprob,
                        "compression_ratio": segment.compression_ratio,
                        "no_speech_prob": segment.no_speech_prob,
                    }
                )
                for word_index, word in enumerate(segment.words or []):
                    clip_word_count += 1
                    word_rows.append(
                        {
                            "clip_id": clip_id,
                            "segment_id": segment_id,
                            "segment_index": segment_index,
                            "word_index": word_index,
                            "word": word.word,
                            "start_sec": word.start,
                            "end_sec": word.end,
                            "probability": getattr(word, "probability", None),
                        }
                    )
            text = " ".join(text_parts).strip()
            clip_row = {
                "clip_id": clip_id,
                "audio_path": str(clip_path),
                "duration_sec": round(float(wav_info["duration_sec"]), 6),
                "sample_rate": wav_info["sample_rate"],
                "channels": wav_info["channels"],
                "text": text,
                "language": info.language,
                "language_probability": info.language_probability,
                "transcription_runtime_sec": round(clip_elapsed, 6),
                "segment_count": len(segments),
                "word_count": clip_word_count,
                "empty_transcript": not bool(text),
            }
            clip_rows.append(clip_row)
            transcript_jsonl_rows.append({**clip_row, "segments": [
                {
                    "start_sec": segment.start,
                    "end_sec": segment.end,
                    "text": segment.text,
                    "avg_logprob": segment.avg_logprob,
                    "compression_ratio": segment.compression_ratio,
                    "no_speech_prob": segment.no_speech_prob,
                    "words": [
                        {
                            "word": word.word,
                            "start_sec": word.start,
                            "end_sec": word.end,
                            "probability": getattr(word, "probability", None),
                        }
                        for word in (segment.words or [])
                    ],
                }
                for segment in segments
            ]})
            print(json.dumps({"clip": clip_index + 1, "total": len(clip_paths), "clip_id": clip_id, "words": clip_word_count, "runtime_sec": round(clip_elapsed, 3)}), flush=True)
    finally:
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    total_elapsed = time.perf_counter() - started
    write_csv(
        out_dir / "clip_transcripts.csv",
        clip_rows,
        [
            "clip_id",
            "audio_path",
            "duration_sec",
            "sample_rate",
            "channels",
            "text",
            "language",
            "language_probability",
            "transcription_runtime_sec",
            "segment_count",
            "word_count",
            "empty_transcript",
        ],
    )
    write_csv(
        out_dir / "segments.csv",
        segment_rows,
        [
            "clip_id",
            "segment_id",
            "segment_index",
            "start_sec",
            "end_sec",
            "text",
            "avg_logprob",
            "compression_ratio",
            "no_speech_prob",
        ],
    )
    write_csv(
        out_dir / "word_probabilities.csv",
        word_rows,
        [
            "clip_id",
            "segment_id",
            "segment_index",
            "word_index",
            "word",
            "start_sec",
            "end_sec",
            "probability",
        ],
    )
    append_jsonl(out_dir / "transcripts.jsonl", transcript_jsonl_rows)
    summary = {
        "model": args.model,
        "backend": "faster-whisper",
        "faster_whisper_version": faster_whisper.__version__,
        "device": args.device,
        "compute_type": args.compute_type,
        "language": args.language,
        "beam_size": args.beam_size,
        "word_timestamps": True,
        "vad_filter": args.vad_filter,
        "condition_on_previous_text": args.condition_on_previous_text,
        "clip_count": len(clip_rows),
        "empty_transcript_count": sum(row["empty_transcript"] for row in clip_rows),
        "segment_count": len(segment_rows),
        "word_count": len(word_rows),
        "audio_duration_sec": round(sum(float(row["duration_sec"]) for row in clip_rows), 6),
        "model_load_sec": round(load_elapsed, 6),
        "total_runtime_sec": round(total_elapsed, 6),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
