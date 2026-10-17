import os
import unittest
from unittest import mock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from speechcraft_dataset.asr import (
    AsrError,
    UnsupportedAsrOption,
    expected_backend_for_platform,
    mlx_transcribe_options,
    normalize_faster_whisper_result,
    normalize_mlx_payload,
    resolve_managed_backend,
    select_asr_backend,
)
from speechcraft_dataset.analyze_whisper_b1_transcript_qc import serialize_segments


class NormalizeTests(unittest.TestCase):
    def test_mlx_and_faster_whisper_shapes_score_the_same_timestamps(self):
        mlx = normalize_mlx_payload({
            "text": "hello world",
            "language": "en",
            "segments": [{
                "start": 0.0, "end": 1.2, "text": " hello world",
                "avg_logprob": -0.2, "compression_ratio": 1.1, "no_speech_prob": 0.01,
                "words": [
                    {"word": " hello", "start": 0.0, "end": 0.4, "probability": 0.9},
                    {"word": " world", "start": 0.5, "end": 1.2, "probability": 0.8},
                ],
            }],
        }, model_identity="mlx-fixture")
        faster = normalize_faster_whisper_result([
            SimpleNamespace(text=" hello world", start=0.0, end=1.2, avg_logprob=-0.2,
                            compression_ratio=1.1, no_speech_prob=0.01, words=[
                                SimpleNamespace(word=" hello", start=0.0, end=0.4, probability=0.9),
                                SimpleNamespace(word=" world", start=0.5, end=1.2, probability=0.8),
                            ]),
        ], SimpleNamespace(language="en"), model_identity="ct2-fixture")
        self.assertEqual(serialize_segments(mlx.segments), serialize_segments(faster.segments))
        self.assertEqual(mlx.info.language, "en")
        self.assertEqual(mlx.info.backend, "mlx-whisper")
        self.assertEqual(faster.info.backend, "faster-whisper/ctranslate2")
        self.assertEqual(mlx.info.metadata_gaps, [])

    def test_multilingual_language_is_preserved(self):
        result = normalize_mlx_payload({
            "language": "hi",
            "segments": [{"start": 0, "end": 1, "text": " namaste", "no_speech_prob": 0.02,
                          "words": [{"word": " namaste", "start": 0, "end": 1, "probability": 0.7}]}],
        }, model_identity="mlx")
        self.assertEqual(result.info.language, "hi")

    def test_empty_transcript_has_no_invented_text(self):
        result = normalize_mlx_payload({"language": "en", "segments": [], "text": ""}, model_identity="mlx")
        self.assertEqual(result.segments, [])
        self.assertIn("no_speech_prob", result.info.metadata_gaps)

    def test_missing_no_speech_stays_missing(self):
        result = normalize_mlx_payload({
            "language": "en",
            "segments": [{"start": 0, "end": 1, "text": " quiet",
                          "words": [{"word": " quiet", "start": 0, "end": 1, "probability": 0.4}]}],
        }, model_identity="mlx")
        self.assertIsNone(result.segments[0].no_speech_prob)
        self.assertEqual(result.info.metadata_gaps, ["no_speech_prob"])

    def test_high_no_speech_is_copied_not_rewritten(self):
        result = normalize_mlx_payload({
            "segments": [{"start": 0, "end": 0.5, "text": " hmm", "no_speech_prob": 0.91,
                          "words": [{"word": " hmm", "start": 0, "end": 0.5, "probability": 0.2}]}],
        }, model_identity="mlx")
        self.assertEqual(result.segments[0].no_speech_prob, 0.91)

    def test_invalid_timestamps_are_rejected(self):
        with self.assertRaises(AsrError):
            normalize_mlx_payload({
                "segments": [{"start": 2, "end": 1, "text": " backwards", "no_speech_prob": 0.1, "words": []}],
            }, model_identity="mlx")

    def test_mlx_options_forward_beam_size_and_reject_vad(self):
        translated = mlx_transcribe_options({
            "task": "transcribe", "vad_filter": False, "word_timestamps": True,
            "condition_on_previous_text": False, "beam_size": 5, "language": "en",
        })
        self.assertEqual(translated["beam_size"], 5)
        self.assertNotIn("vad_filter", translated)
        self.assertEqual(translated["word_timestamps"], True)
        self.assertEqual(translated["language"], "en")
        with self.assertRaises(UnsupportedAsrOption):
            mlx_transcribe_options({"vad_filter": True, "beam_size": 5})
        with self.assertRaises(UnsupportedAsrOption):
            mlx_transcribe_options({"beam_size": 5, "initial_prompt": "secret"})
        with self.assertRaises(UnsupportedAsrOption):
            mlx_transcribe_options({"task": "transcribe", "vad_filter": False, "word_timestamps": True,
                                    "condition_on_previous_text": False})

    def test_backend_selection_stays_offline_and_refuses_the_wrong_snapshot(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            mlx = root / "mlx"
            mlx.mkdir()
            (mlx / "weights.npz").write_bytes(b"weights")
            (mlx / "config.json").write_bytes(b"{}")
            ct2 = root / "ct2"
            ct2.mkdir()
            (ct2 / "model.bin").write_bytes(b"bin")
            self.assertEqual(select_asr_backend(mlx), "mlx")
            self.assertEqual(select_asr_backend(ct2), "ctranslate2")
            with self.assertRaises(AsrError):
                select_asr_backend(root)

    def test_managed_backend_follows_the_platform_not_incidental_files(self):
        self.assertEqual(expected_backend_for_platform("macos-arm64"), "mlx")
        self.assertEqual(expected_backend_for_platform("windows-wsl2"), "ctranslate2")
        self.assertEqual(expected_backend_for_platform("linux-x86_64"), "ctranslate2")
        with TemporaryDirectory() as temp:
            root = Path(temp)
            mlx = root / "mlx"
            mlx.mkdir()
            (mlx / "weights.npz").write_bytes(b"w")
            (mlx / "config.json").write_bytes(b"{}")
            ct2 = root / "ct2"
            ct2.mkdir()
            (ct2 / "model.bin").write_bytes(b"b")
            both = root / "both"
            both.mkdir()
            (both / "weights.npz").write_bytes(b"w")
            (both / "config.json").write_bytes(b"{}")
            (both / "model.bin").write_bytes(b"b")
            with mock.patch.dict(os.environ, {"SPEECHCRAFT_MANAGED_RUNTIME": "1", "SPEECHCRAFT_MODEL_PLATFORM": "macos-arm64", "SPEECHCRAFT_ASR_BACKEND": "mlx"}):
                self.assertEqual(select_asr_backend(mlx), "mlx")
                with self.assertRaises(AsrError):
                    select_asr_backend(both)
            with mock.patch.dict(os.environ, {"SPEECHCRAFT_MANAGED_RUNTIME": "1", "SPEECHCRAFT_MODEL_PLATFORM": "windows-wsl2", "SPEECHCRAFT_ASR_BACKEND": "ctranslate2"}):
                self.assertEqual(resolve_managed_backend(ct2), "ctranslate2")
            with mock.patch.dict(os.environ, {"SPEECHCRAFT_MANAGED_RUNTIME": "1", "SPEECHCRAFT_MODEL_PLATFORM": "linux-x86_64", "SPEECHCRAFT_ASR_BACKEND": "mlx"}):
                with self.assertRaises(AsrError):
                    resolve_managed_backend(ct2)
            with mock.patch.dict(os.environ, {"SPEECHCRAFT_MANAGED_RUNTIME": "1", "SPEECHCRAFT_MODEL_PLATFORM": "haiku"}):
                with self.assertRaises(AsrError):
                    resolve_managed_backend(ct2)


if __name__ == "__main__":
    unittest.main()
