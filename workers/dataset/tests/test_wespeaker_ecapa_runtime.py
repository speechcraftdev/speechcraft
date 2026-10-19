from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from speechcraft_dataset.eval_speaker_purity import (
    SpeakerPurityConfig,
    build_sanitized_target_voiceprint,
    cosine_similarity,
    embedding_vector,
    score_candidate_clip,
    window_is_scorable,
)
from speechcraft_dataset.wespeaker_ecapa_runtime import EMBEDDING_DIM, MODEL_ID, wespeaker_features


class WeSpeakerEcapaRuntimeTests(unittest.TestCase):
    def test_same_and_different_speaker_embedding_comparisons_are_cosines(self):
        target = embedding_vector(np.r_[1.0, np.zeros(EMBEDDING_DIM - 1)])
        same_voice = embedding_vector(np.r_[0.8, np.zeros(EMBEDDING_DIM - 1)])
        other_voice = embedding_vector(np.r_[0.0, 1.0, np.zeros(EMBEDDING_DIM - 2)])
        self.assertAlmostEqual(cosine_similarity(target, same_voice), 1.0)
        self.assertAlmostEqual(cosine_similarity(target, other_voice), 0.0)
        self.assertEqual(MODEL_ID, "wespeaker-ecapa-tdnn512-lm")

    def test_short_clip_is_unscored_without_fabricated_similarity(self):
        class MustNotInfer:
            def infer_segment(self, _samples):
                raise AssertionError("too-short audio must not reach the model")

        result = score_candidate_clip(
            MustNotInfer(), np.ones(4000, dtype=np.float32) * 0.1, 16000,
            np.r_[1.0, np.zeros(EMBEDDING_DIM - 1)], SpeakerPurityConfig(),
        )
        self.assertIsNone(result["purity_score"])
        self.assertEqual(result["scored_window_count"], 0)
        self.assertEqual(result["reason_codes"], ["no_scorable_windows"])

    def test_silence_is_unscored_without_zero_similarity(self):
        class MustNotInfer:
            def infer_segment(self, _samples):
                raise AssertionError("silence must not reach the model")

        result = score_candidate_clip(
            MustNotInfer(), np.zeros(3 * 16000, dtype=np.float32), 16000,
            np.r_[1.0, np.zeros(EMBEDDING_DIM - 1)], SpeakerPurityConfig(),
        )
        self.assertIsNone(result["purity_score"])
        self.assertEqual(result["scored_window_count"], 0)
        self.assertGreater(result["skipped_window_count"], 0)

    def test_invalid_audio_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-finite"):
            wespeaker_features(np.asarray([0.0, np.nan] * 5000, dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            window_is_scorable(np.asarray([0.0, np.inf] * 5000, dtype=np.float32), 16000, SpeakerPurityConfig())

    def test_insufficient_enrollment_fails_instead_of_building_zero_voiceprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "zero surviving chunks"):
                build_sanitized_target_voiceprint(
                    object(), [], target_speaker_id="speaker_0",
                    analysis_path_by_source={}, sample_rate_by_source={},
                    config=SpeakerPurityConfig(), enrollment_dir=Path(temporary),
                )


if __name__ == "__main__":
    unittest.main()
