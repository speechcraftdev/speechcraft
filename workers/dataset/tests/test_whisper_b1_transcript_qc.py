from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from speechcraft_dataset.analyze_whisper_b1_transcript_qc import (
    TRANSCRIPT_SCORE_METHOD,
    apply_review_policy,
    detect_number_symbol_hazards,
    run_transcript_qc,
    score_bucket,
    score_bucket_hint,
    score_candidate_clip,
)
from speechcraft_dataset.b1_lj_score import (
    extract_lexical_words,
    m1_m2_gap_from_probs,
    normalize_lexical_token,
    normalize_probabilities_0_100,
    predict_b1_lj_score,
    score_b1_lj_from_probs,
)
from speechcraft_dataset.buffers import write_pcm16_mono
from speechcraft_dataset.qc_score_stages import run_transcript_qc_stage


class FakeWord:
    def __init__(self, word: str, probability: float, start: float = 0.0, end: float = 0.2) -> None:
        self.word = word
        self.probability = probability
        self.start = start
        self.end = end


class FakeSegment:
    def __init__(
        self,
        text: str,
        words: list[FakeWord],
        *,
        no_speech_prob: float = 0.01,
        avg_logprob: float = -0.2,
        compression_ratio: float = 1.1,
        start: float = 0.0,
        end: float = 1.0,
    ) -> None:
        self.text = text
        self.words = words
        self.no_speech_prob = no_speech_prob
        self.avg_logprob = avg_logprob
        self.compression_ratio = compression_ratio
        self.start = start
        self.end = end


class CountingFakeModel:
    def __init__(self, segments_by_call: list[list[FakeSegment]] | None = None) -> None:
        self.create_count = 0
        self.transcribe_calls: list[dict] = []
        self._segments_by_call = segments_by_call or []

    def __call__(self, model_reference: str, device: str, compute_type: str) -> "CountingFakeModel":
        self.create_count += 1
        self.model_reference = model_reference
        self.device = device
        self.compute_type = compute_type
        return self

    def transcribe(self, audio_path: str, **kwargs):
        self.transcribe_calls.append({"audio_path": audio_path, **kwargs})
        index = min(len(self.transcribe_calls) - 1, max(0, len(self._segments_by_call) - 1))
        segments = self._segments_by_call[index] if self._segments_by_call else [
            FakeSegment("hello world", [FakeWord("hello", 0.9), FakeWord("world", 0.8)])
        ]
        return segments, object()


class B1LjScoreTests(unittest.TestCase):
    def test_frozen_reference_examples(self) -> None:
        harmful, score = predict_b1_lj_score(70.0, 85.0)
        self.assertEqual(harmful, 0.05884092)
        self.assertEqual(score, 94.1159)

        harmful, score = predict_b1_lj_score(100.0, 100.0)
        self.assertEqual(harmful, 0.0158556)
        self.assertEqual(score, 98.4144)

        result = score_b1_lj_from_probs([90.0, 80.0, 95.0])
        self.assertEqual(result.m1, 80.0)
        self.assertEqual(result.m2, 90.0)
        self.assertEqual(result.gap, 10.0)
        expected_harmful, expected_score = predict_b1_lj_score(80.0, 90.0)
        self.assertEqual(result.harmful_probability, expected_harmful)
        self.assertEqual(result.transcript_match_score, expected_score)
        self.assertEqual(result.harmful_probability, 0.03826268)
        self.assertEqual(result.transcript_match_score, 96.1737)

    def test_unit_probabilities_normalize(self) -> None:
        self.assertEqual(normalize_probabilities_0_100([0.5, 0.9]), [50.0, 90.0])

    def test_percent_probabilities_normalize(self) -> None:
        self.assertEqual(normalize_probabilities_0_100([50.0, 90.0]), [50.0, 90.0])

    def test_mixed_probability_ranges_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "mixed probability scales"):
            normalize_probabilities_0_100([0.5, 90.0])

    def test_out_of_range_values_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "out of range"):
            normalize_probabilities_0_100([-0.1])
        with self.assertRaisesRegex(ValueError, "out of range"):
            normalize_probabilities_0_100([100.1])

    def test_nan_and_infinite_values_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            normalize_probabilities_0_100([float("nan")])
        with self.assertRaisesRegex(ValueError, "finite"):
            normalize_probabilities_0_100([float("inf")])

    def test_one_lexical_word(self) -> None:
        result = score_b1_lj_from_probs([88.0])
        self.assertEqual(result.m1, 88.0)
        self.assertEqual(result.m2, 88.0)
        self.assertEqual(result.gap, 0.0)

    def test_multiple_lexical_words(self) -> None:
        m1, m2, gap = m1_m2_gap_from_probs([70.0, 95.0, 80.0])
        self.assertEqual((m1, m2, gap), (70.0, 80.0, 10.0))

    def test_punctuation_only_tokens_excluded(self) -> None:
        words = extract_lexical_words(
            [
                {"word": "...", "probability": 0.9},
                {"word": "?", "probability": 0.8},
                {"word": "hello", "probability": 0.7},
            ]
        )
        self.assertEqual([word.normalized_text for word in words], ["hello"])
        self.assertEqual([word.probability_0_100 for word in words], [70.0])

    def test_words_wrapped_in_punctuation_remain_lexical(self) -> None:
        self.assertEqual(normalize_lexical_token("(hello)"), "hello")
        self.assertEqual(normalize_lexical_token("don't"), "don't")
        words = extract_lexical_words([{"word": '"World!"', "probability": 0.91}])
        self.assertEqual(words[0].normalized_text, "World")
        self.assertEqual(words[0].raw_text, '"World!"')

    def test_empty_probs_rejected_by_pure_scorer(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty lexical probability list"):
            score_b1_lj_from_probs([])


class WhisperB1TranscriptQcTests(unittest.TestCase):
    def _write_manifest(self, run_root: Path, clips: list[dict]) -> None:
        artifacts = run_root / "artifacts"
        audio_dir = artifacts / "candidate_review_clips"
        audio_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for clip in clips:
            clip_id = clip["id"]
            rel = f"artifacts/candidate_review_clips/{clip_id}.wav"
            write_pcm16_mono(run_root / rel, [0.0] * 16000, 16000)
            row = {
                "id": clip_id,
                "audio_path": rel,
                "duration_sec": 1.0,
                "buffer_id": "buffer_000000",
                "word_ids": [],
                "review_reason_codes": [],
                "training_text": clip.get("training_text", "hello world"),
                "alignment_text": clip.get("alignment_text", "hello world"),
            }
            rows.append(row)
        (artifacts / "candidate_review_manifest.json").write_text(json.dumps(rows), encoding="utf-8")

    def test_zero_lexical_words_unscored_and_review_required(self) -> None:
        factory = CountingFakeModel(
            segments_by_call=[[FakeSegment("...", [FakeWord("...", 0.99), FakeWord("!!!", 0.98)])]]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            with patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_transcript_qc_model_reference",
                return_value=("large-v3", "/tmp/fake-whisper"),
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_device",
                return_value="cpu",
            ):
                summary = run_transcript_qc(run_root, {}, model_factory=factory)

            artifact = json.loads((run_root / "artifacts" / "transcript_qc.json").read_text(encoding="utf-8"))
            row = artifact["clips"][0]
            self.assertIsNone(row["transcript_match_score"])
            self.assertNotEqual(row["transcript_match_score"], 100)
            self.assertTrue(row["review_required"])
            self.assertIn("no_lexical_words", row["reason_codes"])
            self.assertEqual(row["bucket_hint"], "unscored")
            self.assertEqual(summary["failed_count"], 1)
            self.assertEqual(summary["scored_count"], 0)

    def test_high_no_speech_adds_review_reason_without_mutating_score(self) -> None:
        words = [FakeWord("hello", 0.9), FakeWord("world", 0.8)]
        expected = score_b1_lj_from_probs([90.0, 80.0])
        factory = CountingFakeModel(
            segments_by_call=[[FakeSegment("hello world", words, no_speech_prob=0.85)]]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            with patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_transcript_qc_model_reference",
                return_value=("large-v3", "/tmp/fake-whisper"),
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_device",
                return_value="cpu",
            ):
                run_transcript_qc(run_root, {}, model_factory=factory)
            row = json.loads((run_root / "artifacts" / "transcript_qc.json").read_text(encoding="utf-8"))["clips"][0]
            self.assertEqual(row["transcript_match_score"], expected.transcript_match_score)
            self.assertEqual(row["m1"], expected.m1)
            self.assertEqual(row["m2"], expected.m2)
            self.assertIn("high_no_speech_prob", row["reason_codes"])
            self.assertTrue(row["review_required"])

    def test_number_symbol_hazards_are_separate(self) -> None:
        hazards = detect_number_symbol_hazards("pay $20 now", "clean text")
        self.assertIn("contains_numeric_token", hazards)
        self.assertIn("contains_danger_symbol", hazards)
        self.assertIn("contains_currency_symbol", hazards)
        review_required, reasons = apply_review_policy(
            lexical_count=2,
            score=95.0,
            high_no_speech_segments=[],
            hazard_flags=hazards,
        )
        self.assertTrue(review_required)
        self.assertIn("contains_numeric_token", reasons)
        # Score math stays independent of hazard policy.
        result = score_b1_lj_from_probs([90.0, 95.0])
        self.assertGreater(result.transcript_match_score, 0.0)

    def test_model_initialized_once_across_multiple_clips(self) -> None:
        factory = CountingFakeModel(
            segments_by_call=[
                [FakeSegment("hello", [FakeWord("hello", 0.9)])],
                [FakeSegment("world", [FakeWord("world", 0.85)])],
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(
                run_root,
                [
                    {"id": "candidate_review_clip_000000"},
                    {"id": "candidate_review_clip_000001"},
                ],
            )
            with patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_transcript_qc_model_reference",
                return_value=("large-v3", "/tmp/fake-whisper"),
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_device",
                return_value="cpu",
            ):
                summary = run_transcript_qc(run_root, {}, model_factory=factory)
            self.assertEqual(factory.create_count, 1)
            self.assertEqual(len(factory.transcribe_calls), 2)
            self.assertEqual(summary["clip_count"], 2)

    def test_required_transcription_options_are_passed(self) -> None:
        factory = CountingFakeModel()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            with patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_transcript_qc_model_reference",
                return_value=("large-v3", "/tmp/fake-whisper"),
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_device",
                return_value="cpu",
            ):
                run_transcript_qc(run_root, {}, model_factory=factory)
            kwargs = factory.transcribe_calls[0]
            self.assertTrue(kwargs["word_timestamps"])
            self.assertFalse(kwargs["vad_filter"])
            self.assertFalse(kwargs["condition_on_previous_text"])
            self.assertEqual(kwargs["task"], "transcribe")

    def test_candidate_identifiers_and_output_contract_preserved(self) -> None:
        factory = CountingFakeModel()
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            with patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_transcript_qc_model_reference",
                return_value=("large-v3", "/tmp/fake-whisper"),
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.resolve_device",
                return_value="cpu",
            ), patch(
                "speechcraft_dataset.analyze_whisper_b1_transcript_qc.create_whisper_model",
                side_effect=factory,
            ):
                summary = run_transcript_qc_stage(run_root, {})
            artifact = json.loads((run_root / "artifacts" / "transcript_qc.json").read_text(encoding="utf-8"))
            self.assertEqual(artifact["schema_version"], 1)
            self.assertEqual(artifact["stage"], "transcript_qc")
            self.assertEqual(artifact["score_method"], TRANSCRIPT_SCORE_METHOD)
            row = artifact["clips"][0]
            self.assertEqual(row["clip_id"], "candidate_review_clip_000000")
            self.assertEqual(row["audio_path"], "artifacts/candidate_review_clips/candidate_review_clip_000000.wav")
            self.assertEqual(row["transcript_score_method"], TRANSCRIPT_SCORE_METHOD)
            self.assertIn("transcript_match_score", row)
            self.assertIn("reason_codes", row)
            self.assertIn("bucket_hint", row)
            self.assertEqual(summary["score_method"], TRANSCRIPT_SCORE_METHOD)
            self.assertEqual(factory.create_count, 1)

    def test_removed_ctc_backend_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            with self.assertRaisesRegex(ValueError, "CTC transcript scoring has been removed"):
                run_transcript_qc(run_root, {"transcript_qc_backend": "ctc"})
            with self.assertRaisesRegex(ValueError, "CTC transcript scoring has been removed"):
                run_transcript_qc(run_root, {"transcript_qc_model": "facebook/wav2vec2-base-960h"})

    def test_old_ctc_modules_are_gone(self) -> None:
        import importlib

        for module_name in (
            "speechcraft_dataset.analyze_ctc_transcript_qc",
            "speechcraft_dataset.ctc_transcript_torture_test",
            "speechcraft_dataset.transcript_scoring_features",
        ):
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_score_bucket_helpers(self) -> None:
        self.assertEqual(score_bucket(90.0), "accepted")
        self.assertEqual(score_bucket(80.0), "review")
        self.assertEqual(score_bucket(50.0), "rejected")
        self.assertEqual(score_bucket(None), "failed")
        self.assertEqual(score_bucket_hint(None), "unscored")

    def test_score_candidate_clip_includes_identity_in_errors(self) -> None:
        class BoomModel:
            def transcribe(self, *_args, **_kwargs):
                raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as temp_dir:
            run_root = Path(temp_dir)
            self._write_manifest(run_root, [{"id": "candidate_review_clip_000000"}])
            candidate = json.loads(
                (run_root / "artifacts" / "candidate_review_manifest.json").read_text(encoding="utf-8")
            )[0]
            row = score_candidate_clip(
                candidate,
                run_root,
                BoomModel(),
                language=None,
                beam_size=5,
                transcribe_timeout_sec=None,
            )
            self.assertIn("candidate_review_clip_000000", row["error"])
            self.assertIn("whisper_scoring_failed", row["reason_codes"])


if __name__ == "__main__":
    unittest.main()
