from __future__ import annotations

import unittest

from app.defaults import (
    DATASET_PROCESSING_DEFAULTS,
    DATASET_SLICER_HARDCODED,
    build_dataset_worker_config,
    build_slicer_config_overrides,
    resolve_asr_device_and_compute_type,
    resolve_whisper_model,
)


class DefaultsTests(unittest.TestCase):
    def test_build_slicer_config_locks_vr_geometry(self) -> None:
        config = build_slicer_config_overrides({"cutpoint_frame_ms": 99, "candidate_target_clip_sec": 5.0})
        self.assertNotIn("cutpoint_frame_ms", config)
        self.assertEqual(config["slicer"], "VR")
        self.assertEqual(config["slicer_geometry"], "O0_4")
        self.assertEqual(config["candidate_target_clip_sec"], 8.0)
        self.assertEqual(config["candidate_min_clip_sec"], 3.0)
        self.assertEqual(config["candidate_max_clip_sec"], 15.0)

    def test_leftover_ui_keys_are_accepted_without_retuning_o0_4(self) -> None:
        config = build_slicer_config_overrides({"cutpoint_min_gap_ms": 40, "candidate_max_clip_sec": 12.0})
        self.assertEqual(config["cutpoint_min_gap_ms"], 40)
        self.assertEqual(config["candidate_max_clip_sec"], 15.0)

    def test_build_dataset_worker_config(self) -> None:
        config = build_dataset_worker_config(language="en", whisper_model_size="large-v3")

        self.assertEqual(config["slicer"], "VR")
        self.assertEqual(config["slicer_geometry"], "O0_4")
        self.assertEqual(config["geometry_fingerprint"], DATASET_SLICER_HARDCODED["geometry_fingerprint"])
        self.assertEqual(config["candidate_target_clip_sec"], 8.0)
        self.assertNotIn("mfa_dictionary", config)
        self.assertNotIn("mfa_acoustic_model", config)
        self.assertNotIn("max_processing_buffer_sec", config)
        self.assertEqual(config["faster_whisper_beam_size"], 5)
        self.assertEqual(config["asr_condition_on_previous_text"], False)
        self.assertEqual(config["asr_word_timestamps"], True)

    def test_auto_language_uses_whisper_auto_detect(self) -> None:
        config = build_dataset_worker_config(language="auto", whisper_model_size="base")
        self.assertEqual(config["asr_language"], "auto")
        self.assertEqual(config["faster_whisper_model"], "base")
        self.assertNotIn("mfa_dictionary", config)

    def test_explicit_language_sets_asr_language_without_mfa(self) -> None:
        config = build_dataset_worker_config(language="fr", whisper_model_size="large-v3")
        self.assertEqual(config["asr_language"], "fr")
        self.assertNotIn("mfa_dictionary", config)
        self.assertNotIn("mfa_acoustic_model", config)

    def test_overrides_cannot_retune_locked_vr_bounds(self) -> None:
        config = build_dataset_worker_config(
            language="en",
            whisper_model_size="large-v3",
            overrides={"vad_threshold": 0.6, "candidate_target_clip_sec": 5.0},
        )
        self.assertEqual(config["vad_threshold"], 0.6)
        self.assertEqual(config["candidate_target_clip_sec"], 8.0)
        self.assertEqual(config["faster_whisper_beam_size"], DATASET_PROCESSING_DEFAULTS["faster_whisper_beam_size"])

    def test_resolve_whisper_model_size(self) -> None:
        self.assertEqual(resolve_whisper_model("large-v3"), "large-v3")
        self.assertEqual(resolve_whisper_model("base"), "base")

    def test_resolve_asr_device_and_compute_type(self) -> None:
        device, compute_type = resolve_asr_device_and_compute_type()
        self.assertIn(device, {"cuda", "cpu"})
        self.assertIn(compute_type, {"float16", "int8", "float32"})


if __name__ == "__main__":
    unittest.main()
