from __future__ import annotations

import math
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from speechcraft_dataset.reference_quality import (
    calculate_reference_quality,
    duration_preference,
    position_preference,
    run_reference_quality,
    METHOD,
    WEIGHTS,
)


class ReferenceQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = {
            "id": "c1", "source_audio_id": "source-a", "source_start_sample": 30000,
            "source_end_sample": 38000, "sample_rate": 1000,
            "duration_samples": 8000, "duration_sec": 8.0,
        }
        self.transcript_rows = [{"clip_id": "c1", "transcript_match_score": 80.0}]

    def score(self, speaker_rows, regions=None, target_speaker_id=None, rates=None, transcript_rows=None):
        return calculate_reference_quality(
            [self.candidate], speaker_rows, regions or [],
            self.transcript_rows if transcript_rows is None else transcript_rows,
            target_speaker_id, rates or {},
        )

    def test_exact_weighted_calculation_and_70_30_representativeness(self) -> None:
        rows = self.score(
            [{"clip_id": "c1", "mean_window_similarity": 0.8,
              "p10_window_similarity": 0.6, "purity_score": 0.123}],
        )
        self.assertAlmostEqual(rows[0]["representativeness"], 0.74)
        self.assertAlmostEqual(rows[0]["duration_preference"], 1.0)
        self.assertAlmostEqual(rows[0]["position_preference"], 0.5)
        self.assertAlmostEqual(rows[0]["transcript_confidence"], 0.8)
        self.assertAlmostEqual(rows[0]["reference_quality_score"], 73.5)

    def test_duration_plateau_and_gradual_falloff(self) -> None:
        self.assertEqual([duration_preference(v) for v in (6, 7, 9)], [1, 1, 1])
        self.assertAlmostEqual(duration_preference(4), 0.84)
        self.assertAlmostEqual(duration_preference(11), 0.92)
        self.assertEqual(duration_preference(0), 0.52)
        self.assertEqual(duration_preference(40), 0.0)

    def test_position_inside_region_uses_boundary_margin_not_midpoint(self) -> None:
        regions = [{"source_audio_id": "source-a", "speaker_id": "spk", "start_sample": 0, "end_sample": 50000}]
        score, available = position_preference(self.candidate, regions, "spk", {"source-a": 1000})
        self.assertEqual((score, available), (1.0, True))
        another = dict(self.candidate, source_start_sample=10000, source_end_sample=18000)
        self.assertEqual(position_preference(another, regions, "spk", {"source-a": 1000}), (1.0, True))

    def test_boundary_touch_or_crossing_is_zero_and_recordings_are_isolated(self) -> None:
        regions = [{"source_audio_id": "source-a", "speaker_id": "spk", "start_sample": 0, "end_sample": 35000}]
        self.assertEqual(position_preference(self.candidate, regions, "spk", {"source-a": 1000}), (0.0, True))
        other = dict(self.candidate, source_audio_id="source-b")
        self.assertEqual(position_preference(other, regions, "spk", {"source-a": 1000, "source-b": 1000}), (0.5, False))

    def test_overlapping_regions_choose_best_contained_margin_and_missing_is_neutral(self) -> None:
        regions = [
            {"source_audio_id": "source-a", "speaker_id": "spk", "start_sample": 0, "end_sample": 35000},
            {"source_audio_id": "source-a", "speaker_id": "spk", "start_sample": 10000, "end_sample": 50000},
        ]
        score, available = position_preference(self.candidate, regions, "spk", {"source-a": 1000})
        self.assertEqual((score, available), (1.0, True))
        self.assertEqual(position_preference(self.candidate, [], "spk", {}), (0.5, False))

    def test_invalid_similarity_is_unscored_and_values_are_finite_bounded(self) -> None:
        for value in (None, float("nan"), float("inf"), "bad"):
            result = self.score(
                [{"clip_id": "c1", "mean_window_similarity": value,
                 "p10_window_similarity": 0.8}],
            )[0]
            self.assertIsNone(result["reference_quality_score"])
        result = self.score(
            [{"clip_id": "c1", "mean_window_similarity": 3.0,
              "p10_window_similarity": 2.0}],
        )[0]
        self.assertTrue(math.isfinite(result["reference_quality_score"]))
        self.assertGreaterEqual(result["reference_quality_score"], 0)
        self.assertLessEqual(result["reference_quality_score"], 100)

    def test_module_has_no_model_initialization_or_inference_dependency(self) -> None:
        with patch.dict("sys.modules", {"speechcraft_dataset.eval_speaker_purity": None}):
            rows = self.score(
                [{"clip_id": "c1", "mean_window_similarity": 0.8,
                 "p10_window_similarity": 0.6}],
            )
        self.assertIsNotNone(rows[0]["reference_quality_score"])

    def test_missing_or_invalid_tc_makes_rq_unscored_and_tc_is_higher_is_better(self) -> None:
        speakers = [{"clip_id": "c1", "mean_window_similarity": 0.8, "p10_window_similarity": 0.6}]
        for value in (None, -0.1, 100.1, float("nan"), float("inf")):
            result = self.score(speakers, transcript_rows=[{"clip_id": "c1", "transcript_match_score": value}])[0]
            self.assertIsNone(result["reference_quality_score"])
            self.assertIn("missing_transcript_confidence", result["reason_codes"])
        low = self.score(speakers, transcript_rows=[{"clip_id": "c1", "transcript_match_score": 20}])[0]
        high = self.score(speakers, transcript_rows=[{"clip_id": "c1", "transcript_match_score": 90}])[0]
        self.assertGreater(high["reference_quality_score"], low["reference_quality_score"])

    def test_backfill_reads_saved_tc_and_sp_and_writes_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "candidate_review_manifest.json").write_text(json.dumps([self.candidate]), encoding="utf-8")
            (artifacts / "speaker_purity.json").write_text(json.dumps({"clips": [{
                "clip_id": "c1", "mean_window_similarity": 0.8, "p10_window_similarity": 0.6,
            }]}), encoding="utf-8")
            (artifacts / "transcript_qc.json").write_text(json.dumps({"clips": [{
                "clip_id": "c1", "transcript_match_score": 80.0,
            }]}), encoding="utf-8")
            (artifacts / "speaker_regions.jsonl").write_text(
                json.dumps({"source_audio_id": "source-a", "speaker_id": "spk", "start_sample": 0, "end_sample": 50000}) + "\n",
                encoding="utf-8",
            )
            (artifacts / "speaker_selection.json").write_text(json.dumps({"target_speaker_id": "spk"}), encoding="utf-8")
            (artifacts / "audio_variants_manifest.json").write_text(json.dumps({"variants": [{
                "source_audio_id": "source-a", "analysis_sample_rate": 1000,
            }]}), encoding="utf-8")

            summary = run_reference_quality(root)
            artifact = json.loads((artifacts / "reference_quality.json").read_text(encoding="utf-8"))
            self.assertEqual(summary, {"clip_count": 1, "scored_count": 1})
            self.assertEqual(artifact["schema_version"], 2)
            self.assertEqual(artifact["score_method"], METHOD)
            self.assertEqual(artifact["weights"], WEIGHTS)
            self.assertIn("transcript_qc", artifact["input_sha256"])
            self.assertAlmostEqual(artifact["clips"][0]["reference_quality_score"], 78.5)


if __name__ == "__main__":
    unittest.main()
