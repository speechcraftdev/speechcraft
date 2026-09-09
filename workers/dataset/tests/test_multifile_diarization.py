"""Unit tests for the pure multi-file diarization helpers (option C).

These exercise concat-offset planning, silence-gap handling, and remapping
that keep speaker identity consistent across files. Pyannote is not imported.
"""

import unittest

from speechcraft_dataset.diarization import (
    concat_waveforms_with_gaps,
    plan_concat_layout,
    remap_concat_regions_to_sources,
)


class PlanConcatLayoutTest(unittest.TestCase):
    def test_cumulative_offsets_with_gap(self):
        variants = [
            {"source_audio_id": "source_audio_0000", "path": "a.wav", "analysis_duration_sec": 10.0},
            {"source_audio_id": "source_audio_0001", "path": "b.wav", "analysis_duration_sec": 5.0},
        ]
        layout = plan_concat_layout(variants, gap_sec=1.0)
        self.assertEqual(layout[0]["start_sec"], 0.0)
        self.assertEqual(layout[0]["end_sec"], 10.0)
        self.assertEqual(layout[1]["start_sec"], 11.0)
        self.assertEqual(layout[1]["end_sec"], 16.0)


class ConcatWaveformsWithGapsTest(unittest.TestCase):
    def test_silence_gap_is_inserted_between_files(self):
        import numpy as np

        first = [[0.1, 0.2, 0.3, 0.4]]
        second = [[0.5, 0.6]]
        concat = concat_waveforms_with_gaps([first, second], sample_rate=2, gap_sec=1.0)
        self.assertEqual(concat.shape, (1, 8))
        self.assertTrue(np.allclose(concat[0, :4], [0.1, 0.2, 0.3, 0.4]))
        self.assertTrue(np.allclose(concat[0, 4:6], [0.0, 0.0]))
        self.assertTrue(np.allclose(concat[0, 6:], [0.5, 0.6]))


class RemapRegionsTest(unittest.TestCase):
    def setUp(self):
        self.layout = [
            {"source_audio_id": "source_audio_0000", "path": "a.wav", "start_sec": 0.0, "end_sec": 10.0},
            {"source_audio_id": "source_audio_0001", "path": "b.wav", "start_sec": 11.0, "end_sec": 16.0},
        ]

    def test_regions_mapped_back_to_local_coordinates(self):
        regions = [
            {"speaker_id": "speaker_0", "start_sec": 2.0, "end_sec": 4.0},
            {"speaker_id": "speaker_1", "start_sec": 12.0, "end_sec": 13.0},
        ]
        rows = remap_concat_regions_to_sources(regions, self.layout, sample_rate=16000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_audio_id"], "source_audio_0000")
        self.assertEqual(rows[0]["start_sec"], 2.0)
        self.assertEqual(rows[0]["end_sec"], 4.0)
        self.assertEqual(rows[1]["source_audio_id"], "source_audio_0001")
        self.assertEqual(rows[1]["start_sec"], 1.0)
        self.assertEqual(rows[1]["end_sec"], 2.0)
        self.assertEqual(rows[1]["start_sample"], 16000)

    def test_regions_in_gap_are_dropped(self):
        regions = [{"speaker_id": "speaker_0", "start_sec": 10.2, "end_sec": 10.8}]
        rows = remap_concat_regions_to_sources(regions, self.layout, sample_rate=16000)
        self.assertEqual(rows, [])

    def test_regions_crossing_source_boundary_are_split(self):
        regions = [{"speaker_id": "speaker_0", "start_sec": 9.5, "end_sec": 11.5}]
        rows = remap_concat_regions_to_sources(regions, self.layout, sample_rate=16000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["source_audio_id"], "source_audio_0000")
        self.assertEqual(rows[0]["start_sec"], 9.5)
        self.assertEqual(rows[0]["end_sec"], 10.0)
        self.assertEqual(rows[1]["source_audio_id"], "source_audio_0001")
        self.assertEqual(rows[1]["start_sec"], 0.0)
        self.assertEqual(rows[1]["end_sec"], 0.5)
        self.assertEqual({row["speaker_id"] for row in rows}, {"speaker_0"})

    def test_speaker_identity_preserved_across_files(self):
        regions = [
            {"speaker_id": "speaker_0", "start_sec": 1.0, "end_sec": 3.0},
            {"speaker_id": "speaker_0", "start_sec": 12.0, "end_sec": 14.0},
        ]
        rows = remap_concat_regions_to_sources(regions, self.layout, sample_rate=16000)
        self.assertEqual({r["speaker_id"] for r in rows}, {"speaker_0"})
        self.assertEqual({r["source_audio_id"] for r in rows}, {"source_audio_0000", "source_audio_0001"})


if __name__ == "__main__":
    unittest.main()
