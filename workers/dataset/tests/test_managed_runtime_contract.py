from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from speechcraft_dataset.run import default_config, load_config
from speechcraft_dataset.analyze_whisper_b1_transcript_qc import resolve_transcript_qc_model_reference


class ManagedRuntimeContractTests(unittest.TestCase):
    def test_managed_asr_and_transcript_qc_share_the_verified_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"fixture")
            asr = root / "models" / "whisper"
            silero = root / "models" / "silero"
            diarization = root / "models" / "community-1"
            asr.mkdir(parents=True)
            silero.mkdir(parents=True)
            diarization.mkdir(parents=True)
            env = {
                "SPEECHCRAFT_MANAGED_RUNTIME": "1",
                "SPEECHCRAFT_MODEL_PLATFORM": "linux-x86_64",
                "SPEECHCRAFT_ASR_MODEL_PATH": str(asr),
                "SPEECHCRAFT_SILERO_MODEL_DIR": str(silero),
                "SPEECHCRAFT_SILERO_ONNX_PATH": str(silero / "silero_vad.onnx"),
                "SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH": str(diarization),
                "HF_HUB_OFFLINE": "1",
            }
            with patch.dict(os.environ, env, clear=False), \
                    patch("speechcraft_dataset.run.verify_managed_model") as verify:
                config = load_config(None, [source], single_speaker=False, target_speaker_label="speaker_0")
            self.assertEqual(config["faster_whisper_model"], "large-v3")
            self.assertEqual(config["faster_whisper_model_path"], str(asr))
            self.assertEqual(config["transcript_qc_model"], "large-v3")
            self.assertEqual(config["transcript_qc_model_path"], str(asr))
            self.assertTrue(config["asr_local_files_only"])
            self.assertEqual(verify.call_count, 3)
            requested, resolved = resolve_transcript_qc_model_reference(config)
            self.assertEqual(requested, "large-v3")
            self.assertEqual(resolved, str(asr))

    def test_speaker_purity_passes_managed_model_path_outside_numeric_config(self) -> None:
        from speechcraft_dataset.eval_speaker_purity import run_speaker_purity

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifacts").mkdir()
            model_path = root / "nemo_en_titanet_large.onnx"
            model_path.write_bytes(b"test model")
            observed: dict = {}

            def fake_evaluate(_root, _stage, **kwargs):
                observed.update(kwargs)
                (root / "artifacts" / "_speaker_purity_stage").mkdir(exist_ok=True)
                (root / "artifacts" / "_speaker_purity_stage" / "speaker_purity_qc.json").write_text("[]")
                return {"target_speaker_id": "speaker_0"}

            (root / "artifacts" / "candidate_review_manifest.json").write_text("[]")
            (root / "artifacts" / "speaker_selection.json").write_text('{"target_speaker_id":"speaker_0"}')
            (root / "artifacts" / "speaker_regions.jsonl").write_text("")
            (root / "artifacts" / "audio_variants_manifest.json").write_text('{"variants":[]}')
            with patch("speechcraft_dataset.eval_speaker_purity.evaluate_speaker_purity", side_effect=fake_evaluate):
                run_speaker_purity(root, {"speaker_purity_model_path": str(model_path)})
            self.assertEqual(observed["model_path"], str(model_path))


if __name__ == "__main__":
    unittest.main()
