from __future__ import annotations

import unittest

from speechcraft_dataset.buffers import clamp_regions_to_audio


class TrustedRegionAudioBoundsTests(unittest.TestCase):
    def test_clamps_diarization_tail_to_analysis_waveform(self) -> None:
        regions = [
            {
                "trusted_region_id": "trusted-0",
                "start_sample": 292096,
                "end_sample": 527536,
                "included_region_ids": ["speaker-0"],
            }
        ]

        bounded, clipped_count = clamp_regions_to_audio(regions, 526800)

        self.assertEqual(clipped_count, 1)
        self.assertEqual(bounded[0]["start_sample"], 292096)
        self.assertEqual(bounded[0]["end_sample"], 526800)

    def test_discards_regions_outside_waveform_after_clamping(self) -> None:
        bounded, clipped_count = clamp_regions_to_audio(
            [{"start_sample": 527536, "end_sample": 528000}],
            526800,
        )

        self.assertEqual(bounded, [])
        self.assertEqual(clipped_count, 1)


if __name__ == "__main__":
    unittest.main()
