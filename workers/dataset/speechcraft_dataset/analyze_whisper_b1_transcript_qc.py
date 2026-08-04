"""Clip-level Whisper B1-LJ transcript-confidence QC.

Scores final candidate-review clips only. Does not run Whisper on slicer buffers.
"""

from __future__ import annotations

import argparse
import gc
import json
import re
from pathlib import Path
from typing import Any, Callable

from .b1_lj_score import (
    extract_lexical_words,
    normalize_lexical_token,
    score_b1_lj_from_lexical_words,
)
from .io import read_json_value, resolve_under_root, sha256_file, write_json
from .models import TimeoutError, faster_whisper_repo_id, timeout_after
from .normalization import DANGER_SYMBOLS, hazard_reason_codes

DEFAULT_MODEL = "large-v3"
TRANSCRIPT_QC_SCHEMA_VERSION = 1
TRANSCRIPT_SCORE_METHOD = "whisper_b1_lj_v1"
WORKER_TRANSCRIPT_THRESHOLD_HINT = 85
NO_SPEECH_REVIEW_THRESHOLD = 0.7
NO_LEXICAL_WORDS_REASON = "no_lexical_words"
HIGH_NO_SPEECH_REASON = "high_no_speech_prob"
WHISPER_SCORING_FAILED_REASON = "whisper_scoring_failed"
REMOVED_TC_BACKENDS = frozenset({"ctc", "ctc_legacy", "greedy", "old_tc", "wav2vec2", "wav2vec2_ctc"})
REMOVED_MODEL_MARKERS = ("wav2vec2", "facebook/wav2vec2")

WhisperModelFactory = Callable[[str, str, str], Any]


def log(message: str) -> None:
    print(f"[whisper_b1_transcript_qc] {message}", flush=True)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((quantile / 100.0) * (len(ordered) - 1)))))
    return round(float(ordered[index]), 6)


def score_bucket(transcript_match_score: float | None) -> str:
    if transcript_match_score is None:
        return "failed"
    if transcript_match_score >= WORKER_TRANSCRIPT_THRESHOLD_HINT:
        return "accepted"
    if transcript_match_score >= 70:
        return "review"
    return "rejected"


def score_bucket_hint(transcript_match_score: float | None) -> str:
    if transcript_match_score is None:
        return "unscored"
    if transcript_match_score >= WORKER_TRANSCRIPT_THRESHOLD_HINT:
        return "pass"
    if transcript_match_score >= 70:
        return "review"
    return "fail"


def _reject_removed_tc_config(config: dict[str, Any]) -> None:
    backend_keys = (
        "transcript_qc_backend",
        "transcript_score_backend",
        "transcript_tc_backend",
        "tc_backend",
    )
    for key in backend_keys:
        raw = config.get(key)
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if value in REMOVED_TC_BACKENDS:
            raise ValueError(
                f"CTC transcript scoring has been removed. Use Whisper B1-LJ scoring. "
                f"Unsupported config {key}={raw!r}."
            )
        if value and value not in {"whisper", "whisper_b1_lj", "b1_lj", "b1-lj"}:
            raise ValueError(
                f"Unknown transcript QC backend {key}={raw!r}. "
                "CTC transcript scoring has been removed. Use Whisper B1-LJ scoring."
            )

    model = str(config.get("transcript_qc_model") or "").strip().lower()
    if any(marker in model for marker in REMOVED_MODEL_MARKERS):
        raise ValueError(
            f"CTC transcript scoring has been removed. Use Whisper B1-LJ scoring. "
            f"Unsupported transcript_qc_model={config.get('transcript_qc_model')!r}."
        )


def resolve_device(device_arg: str) -> str:
    if device_arg == "cpu":
        return "cpu"
    import torch

    if device_arg == "cuda":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but not available")
        return "cuda"
    return "cuda" if torch.cuda.is_available() else "cpu"


def resolve_transcript_qc_model_reference(config: dict[str, Any]) -> tuple[str, str]:
    """Return ``(requested_model_name, resolved_model_reference)``."""
    model_path = str(config.get("transcript_qc_model_path") or "").strip()
    model_name = str(config.get("transcript_qc_model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if model_path:
        path = Path(model_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"transcript_qc_model_path does not exist: {path}")
        return model_name, str(path.resolve())

    cache_dir = str(config.get("transcript_qc_cache_dir") or config.get("faster_whisper_cache_dir") or "").strip() or None
    local_only = bool(
        config.get(
            "transcript_qc_local_files_only",
            config.get("asr_local_files_only", True),
        )
    )
    repo_id = faster_whisper_repo_id(model_name)
    try:
        from huggingface_hub import snapshot_download

        snapshot_path = snapshot_download(
            repo_id=repo_id,
            cache_dir=cache_dir,
            local_files_only=local_only,
        )
        return model_name, str(Path(snapshot_path).resolve())
    except Exception as exc:
        if local_only:
            raise FileNotFoundError(
                f"Transcript QC Whisper model {model_name!r} is not available locally ({repo_id}). "
                f"Download it once or set transcript_qc_model_path. "
                f"Original error: {type(exc).__name__}: {exc}"
            ) from exc
        return model_name, model_name


def create_whisper_model(model_reference: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel(model_reference, device=device, compute_type=compute_type)


def detect_number_symbol_hazards(*texts: str) -> list[str]:
    reasons: list[str] = []
    symbols: list[str] = []
    contains_numeric = False
    for text in texts:
        value = str(text or "")
        if re.search(r"\d", value):
            contains_numeric = True
        symbols.extend(character for character in value if character in DANGER_SYMBOLS)
    reasons.extend(hazard_reason_codes(sorted(set(symbols)), contains_numeric))
    return sorted(set(reasons))


def segment_has_lexical_content(segment: Any) -> bool:
    text = str(getattr(segment, "text", "") or "").strip()
    if normalize_lexical_token(text) is not None:
        return True
    words = getattr(segment, "words", None) or []
    for word in words:
        raw = str(getattr(word, "word", "") or getattr(word, "text", "") or "").strip()
        if normalize_lexical_token(raw) is not None:
            return True
    return False


def serialize_segments(segments: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        rows.append(
            {
                "segment_index": index,
                "start_sec": None if segment.start is None else round(float(segment.start), 6),
                "end_sec": None if segment.end is None else round(float(segment.end), 6),
                "text": str(segment.text or "").strip(),
                "avg_logprob": None if segment.avg_logprob is None else round(float(segment.avg_logprob), 6),
                "compression_ratio": (
                    None if segment.compression_ratio is None else round(float(segment.compression_ratio), 4)
                ),
                "no_speech_prob": (
                    None if segment.no_speech_prob is None else round(float(segment.no_speech_prob), 6)
                ),
            }
        )
    return rows


def serialize_raw_words(segments: list[Any]) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for segment in segments:
        for word in getattr(segment, "words", None) or []:
            raw = str(getattr(word, "word", "") or getattr(word, "text", "") or "").strip()
            probability = getattr(word, "probability", None)
            row: dict[str, Any] = {
                "word": raw,
                "probability": None if probability is None else float(probability),
            }
            if getattr(word, "start", None) is not None:
                row["start_sec"] = round(float(word.start), 6)
            if getattr(word, "end", None) is not None:
                row["end_sec"] = round(float(word.end), 6)
            words.append(row)
    return words


def whisper_text_from_segments(segments: list[Any]) -> str:
    parts = [str(segment.text or "").strip() for segment in segments if str(segment.text or "").strip()]
    return " ".join(parts)


def max_no_speech_prob(segments: list[Any]) -> float | None:
    values = [
        float(segment.no_speech_prob)
        for segment in segments
        if getattr(segment, "no_speech_prob", None) is not None
    ]
    if not values:
        return None
    return round(max(values), 6)


def high_no_speech_lexical_segments(segments: list[Any]) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        no_speech = getattr(segment, "no_speech_prob", None)
        if no_speech is None:
            continue
        if float(no_speech) < NO_SPEECH_REVIEW_THRESHOLD:
            continue
        if not segment_has_lexical_content(segment):
            continue
        flagged.append(
            {
                "segment_index": index,
                "text": str(segment.text or "").strip(),
                "no_speech_prob": round(float(no_speech), 6),
            }
        )
    return flagged


def transcribe_clip(
    model: Any,
    audio_path: Path,
    *,
    language: str | None,
    beam_size: int,
    transcribe_timeout_sec: int | None,
    clip_id: str,
) -> tuple[list[Any], Any]:
    try:
        with timeout_after(transcribe_timeout_sec, f"Whisper TC transcription for {clip_id}"):
            segments_iter, info = model.transcribe(
                str(audio_path),
                language=language,
                task="transcribe",
                vad_filter=False,
                word_timestamps=True,
                condition_on_previous_text=False,
                beam_size=beam_size,
            )
            segments = list(segments_iter)
    except TimeoutError as exc:
        raise RuntimeError(
            f"Whisper transcript QC timed out for clip_id={clip_id!r} audio_path={audio_path}: {exc}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Whisper transcript QC failed for clip_id={clip_id!r} audio_path={audio_path}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return segments, info


def apply_review_policy(
    *,
    lexical_count: int,
    score: float | None,
    high_no_speech_segments: list[dict[str, Any]],
    hazard_flags: list[str],
    extra_reasons: list[str] | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if extra_reasons:
        reasons.extend(extra_reasons)
    if lexical_count <= 0:
        reasons.append(NO_LEXICAL_WORDS_REASON)
    if high_no_speech_segments:
        reasons.append(HIGH_NO_SPEECH_REASON)
    reasons.extend(hazard_flags)
    if score is not None and score < WORKER_TRANSCRIPT_THRESHOLD_HINT:
        reasons.append("low_transcript_match")
    unique = sorted(set(reasons))
    review_required = bool(
        lexical_count <= 0 or high_no_speech_segments or hazard_flags or score is None
    )
    return review_required, unique


def build_base_row(candidate: dict[str, Any]) -> dict[str, Any]:
    clip_id = str(candidate.get("id") or candidate.get("clip_id") or "")
    audio_rel = candidate.get("audio_path")
    return {
        "clip_id": clip_id,
        "audio_path": str(audio_rel or ""),
        "duration_sec": candidate.get("duration_sec"),
        "buffer_id": candidate.get("buffer_id"),
        "word_ids": candidate.get("word_ids") or [],
        "review_reason_codes": candidate.get("review_reason_codes") or [],
        "transcript_score_method": TRANSCRIPT_SCORE_METHOD,
        "whisper_text": None,
        "lexical_word_count": 0,
        "raw_word_tokens": [],
        "lexical_words": [],
        "lexical_probabilities": [],
        "m1": None,
        "m2": None,
        "gap": None,
        "b1_harmful_probability": None,
        "transcript_match_score": None,
        "segments": [],
        "no_speech_prob_max": None,
        "high_no_speech_segments": [],
        "number_symbol_hazards": [],
        "weak_spans": [],
        "review_required": True,
        "review_reasons": [],
        "bucket_hint": "unscored",
        "bucket": "failed",
        "reason_codes": [],
    }


def score_candidate_clip(
    candidate: dict[str, Any],
    run_root: Path,
    model: Any,
    *,
    language: str | None,
    beam_size: int,
    transcribe_timeout_sec: int | None,
) -> dict[str, Any]:
    row = build_base_row(candidate)
    clip_id = row["clip_id"]
    audio_rel = row["audio_path"]

    if not audio_rel:
        row["reason_codes"] = ["missing_audio"]
        row["review_reasons"] = ["missing_audio"]
        row["error"] = f"Candidate row missing audio_path for clip_id={clip_id!r}"
        return row

    try:
        audio_path = resolve_under_root(run_root, str(audio_rel))
    except ValueError as exc:
        row["reason_codes"] = ["missing_audio"]
        row["review_reasons"] = ["missing_audio"]
        row["error"] = f"clip_id={clip_id!r}: {exc}"
        return row

    if not audio_path.exists():
        row["reason_codes"] = ["missing_audio"]
        row["review_reasons"] = ["missing_audio"]
        row["error"] = f"Audio file not found for clip_id={clip_id!r}: {audio_path}"
        return row

    try:
        segments, _info = transcribe_clip(
            model,
            audio_path,
            language=language,
            beam_size=beam_size,
            transcribe_timeout_sec=transcribe_timeout_sec,
            clip_id=clip_id or "unknown",
        )
        raw_words = serialize_raw_words(segments)
        lexical = extract_lexical_words(raw_words)
        whisper_text = whisper_text_from_segments(segments)
        candidate_texts = [
            whisper_text,
            str(candidate.get("training_text") or ""),
            str(candidate.get("alignment_text") or ""),
            str(candidate.get("verifier_text") or ""),
            str(candidate.get("normalized_text") or ""),
        ]
        hazard_flags = detect_number_symbol_hazards(*candidate_texts)
        high_no_speech = high_no_speech_lexical_segments(segments)
        no_speech_max = max_no_speech_prob(segments)

        row["whisper_text"] = whisper_text or None
        row["raw_word_tokens"] = raw_words
        row["lexical_word_count"] = len(lexical)
        row["lexical_words"] = [word.normalized_text for word in lexical]
        row["lexical_probabilities"] = [round(word.probability_0_100, 4) for word in lexical]
        row["segments"] = serialize_segments(segments)
        row["no_speech_prob_max"] = no_speech_max
        row["high_no_speech_segments"] = high_no_speech
        row["number_symbol_hazards"] = hazard_flags

        score: float | None = None
        if lexical:
            result = score_b1_lj_from_lexical_words(lexical)
            score = float(result.transcript_match_score)
            row["m1"] = result.m1
            row["m2"] = result.m2
            row["gap"] = result.gap
            row["b1_harmful_probability"] = result.harmful_probability
            row["transcript_match_score"] = score
        else:
            # Explicit unscored path: do not invent m1/m2=100 perfect scores.
            row["transcript_match_score"] = None

        review_required, reasons = apply_review_policy(
            lexical_count=len(lexical),
            score=score,
            high_no_speech_segments=high_no_speech,
            hazard_flags=hazard_flags,
        )
        row["review_required"] = review_required
        row["review_reasons"] = reasons
        row["reason_codes"] = list(reasons)
        row["bucket"] = score_bucket(score)
        row["bucket_hint"] = score_bucket_hint(score)
        return row
    except Exception as exc:  # noqa: BLE001 - per-clip failure isolation
        row["reason_codes"] = [WHISPER_SCORING_FAILED_REASON]
        row["review_reasons"] = [WHISPER_SCORING_FAILED_REASON]
        row["review_required"] = True
        row["error"] = f"clip_id={clip_id!r} audio_path={audio_rel}: {exc}"
        return row


def run_transcript_qc(
    run_root: Path,
    config: dict[str, Any],
    *,
    model_name: str | None = None,
    device: str | None = None,
    model_factory: WhisperModelFactory | None = None,
) -> dict[str, Any]:
    _reject_removed_tc_config(config)
    if model_name is not None:
        config = dict(config)
        config["transcript_qc_model"] = model_name

    requested_model, model_reference = resolve_transcript_qc_model_reference(config)
    device_arg = str(device or config.get("transcript_qc_device") or config.get("faster_whisper_device") or "auto")
    resolved_device = resolve_device(device_arg)
    compute_type = str(
        config.get("transcript_qc_compute_type")
        or config.get("faster_whisper_compute_type")
        or ("float16" if resolved_device == "cuda" else "int8")
    )
    beam_size = int(config.get("transcript_qc_beam_size") or config.get("faster_whisper_beam_size") or 5)
    language_setting = config.get("transcript_qc_language", config.get("asr_language"))
    if language_setting in {None, "", "auto"}:
        language = None
    else:
        language = str(language_setting)
    model_load_timeout = int(config.get("transcript_qc_model_load_timeout_sec") or config.get("asr_model_load_timeout_sec") or 180)
    transcribe_timeout = int(
        config.get("transcript_qc_transcribe_timeout_sec") or config.get("asr_transcribe_timeout_sec") or 600
    )

    factory = model_factory or create_whisper_model

    manifest_path = resolve_under_root(run_root, "artifacts/candidate_review_manifest.json")
    candidates = read_json_value(manifest_path)
    if not isinstance(candidates, list):
        raise ValueError("candidate_review_manifest.json must contain a list")

    model = None
    rows: list[dict[str, Any]] = []
    try:
        try:
            with timeout_after(model_load_timeout, "Whisper transcript QC model load"):
                model = factory(model_reference, resolved_device, compute_type)
        except TimeoutError as exc:
            raise RuntimeError(
                f"Whisper transcript QC model load timed out: requested={requested_model!r}, "
                f"resolved={model_reference!r}, device={resolved_device!r}, compute_type={compute_type!r}: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Whisper transcript QC model unavailable: requested={requested_model!r}, "
                f"resolved={model_reference!r}, device={resolved_device!r}, compute_type={compute_type!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        log(
            f"scoring {len(candidates)} candidate clips with Whisper B1-LJ "
            f"model={requested_model!r} device={resolved_device} compute_type={compute_type}"
        )
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            rows.append(
                score_candidate_clip(
                    candidate,
                    run_root,
                    model,
                    language=language,
                    beam_size=beam_size,
                    transcribe_timeout_sec=transcribe_timeout,
                )
            )
    finally:
        if model is not None:
            del model
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    scored_rows = [row for row in rows if row.get("transcript_match_score") is not None]
    scores = [
        float(row["transcript_match_score"])
        for row in scored_rows
        if isinstance(row.get("transcript_match_score"), (int, float))
    ]
    reason_counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reason_codes") or []:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    bucket_hint_counts = {
        "pass": sum(row.get("bucket_hint") == "pass" for row in rows),
        "review": sum(row.get("bucket_hint") == "review" for row in rows),
        "fail": sum(row.get("bucket_hint") == "fail" for row in rows),
        "unscored": sum(row.get("bucket_hint") == "unscored" for row in rows),
    }

    artifact_payload = {
        "schema_version": TRANSCRIPT_QC_SCHEMA_VERSION,
        "stage": "transcript_qc",
        "model": requested_model,
        "model_reference": model_reference,
        "score_method": TRANSCRIPT_SCORE_METHOD,
        "device": resolved_device,
        "compute_type": compute_type,
        "clips": rows,
    }
    artifact_path = resolve_under_root(run_root, "artifacts/transcript_qc.json")
    write_json(artifact_path, artifact_payload)
    summary = {
        "schema_version": TRANSCRIPT_QC_SCHEMA_VERSION,
        "stage": "transcript_qc",
        "model": requested_model,
        "model_reference": model_reference,
        "score_method": TRANSCRIPT_SCORE_METHOD,
        "device": resolved_device,
        "compute_type": compute_type,
        "clip_count": len(rows),
        "scored_count": len(scored_rows),
        "failed_count": len(rows) - len(scored_rows),
        "score_p50": _percentile(scores, 50),
        "score_p10": _percentile(scores, 10),
        "score_p90": _percentile(scores, 90),
        "bucket_hint_counts": bucket_hint_counts,
        "reason_counts": dict(sorted(reason_counts.items())),
        "input_artifact_hashes": {
            "candidate_review_manifest_json": sha256_file(manifest_path),
        },
    }
    summary_path = resolve_under_root(run_root, "artifacts/transcript_qc_summary.json")
    write_json(summary_path, summary)
    summary["output_hashes"] = {
        "transcript_qc_json": sha256_file(artifact_path),
        "transcript_qc_summary_json": sha256_file(summary_path),
    }
    write_json(summary_path, summary)
    log(f"wrote transcript QC artifacts for {len(rows)} clips")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clip-level Whisper B1-LJ transcript QC for candidate review clips."
    )
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--config", default=None, help="Optional JSON config path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="Allow Hugging Face downloads instead of requiring a local model snapshot.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config: dict[str, Any] = {}
    if args.config:
        config = dict(read_json_value(Path(args.config).expanduser().resolve()))
    config["transcript_qc_model"] = args.model
    config["transcript_qc_device"] = args.device
    if args.allow_download:
        config["transcript_qc_local_files_only"] = False
    summary = run_transcript_qc(
        Path(args.run_root).expanduser().resolve(),
        config,
        model_name=args.model,
        device=args.device,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
