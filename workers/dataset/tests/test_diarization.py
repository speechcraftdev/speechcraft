from __future__ import annotations

import importlib.util
import inspect
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from speechcraft_dataset.buffers import run_processing_buffers
from speechcraft_dataset.diarization import (
    COMMUNITY1_BACKEND,
    annotation_to_speechcraft_regions,
    community1_snapshot_ready,
    resolve_community1_model_path,
    run_diarization,
    speechcraft_speaker_id,
)
from speechcraft_dataset.io import read_json, read_jsonl
from speechcraft_dataset.run import WORKER_STAGE_ORDER, failure_reason_codes

HAS_WORKER_AUDIO_DEPS = bool(importlib.util.find_spec("numpy") and importlib.util.find_spec("soundfile"))


class FakeTurn:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end


class FakeAnnotation:
    def __init__(self, tracks: list[tuple[FakeTurn, None, str]]) -> None:
        self._tracks = tracks

    def itertracks(self, yield_label: bool = True):
        yield from self._tracks


def write_silent_wav(path: Path, *, sample_rate: int = 16000, duration_sec: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


def write_analysis_fixture(run_root: Path, *, source_audio_id: str = "source_audio_0000", duration_sec: float = 2.0) -> Path:
    analysis = run_root / "audio" / "analysis" / f"{source_audio_id}.mono16000.wav"
    write_silent_wav(analysis, sample_rate=16000, duration_sec=duration_sec)
    artifacts = run_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    relative = f"audio/analysis/{source_audio_id}.mono16000.wav"
    variants = list(read_json(artifacts / "audio_variants_manifest.json").get("variants") or []) if (artifacts / "audio_variants_manifest.json").exists() else []
    variants.append(
        {
            "source_audio_id": source_audio_id,
            "path": relative,
            "analysis_sample_rate": 16000,
            "analysis_duration_sec": duration_sec,
        }
    )
    (artifacts / "audio_variants_manifest.json").write_text(json.dumps({"variants": variants}), encoding="utf-8")
    return analysis


class SpeakerIdTests(unittest.TestCase):
    def test_pyannote_labels_map_deterministically(self) -> None:
        self.assertEqual(speechcraft_speaker_id("SPEAKER_00"), "speaker_0")
        self.assertEqual(speechcraft_speaker_id("SPEAKER_03"), "speaker_3")
        self.assertEqual(speechcraft_speaker_id("speaker_1"), "speaker_1")
        self.assertEqual(speechcraft_speaker_id("SPEAKER_00"), speechcraft_speaker_id("SPEAKER_00"))

    def test_unrecognized_label_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "unrecognized pyannote speaker label"):
            speechcraft_speaker_id("alice")


class TimelineConversionTests(unittest.TestCase):
    def test_annotation_converts_to_speechcraft_regions(self) -> None:
        annotation = FakeAnnotation(
            [
                (FakeTurn(0.5, 1.5), None, "SPEAKER_00"),
                (FakeTurn(2.0, 3.25), None, "SPEAKER_01"),
            ]
        )
        rows = annotation_to_speechcraft_regions(
            annotation,
            source_audio_id="source_audio_0000",
            analysis_audio_path="audio/analysis/source_audio_0000.mono16000.wav",
            sample_rate=16000,
            backend_version="4.0.3",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["speaker_id"], "speaker_0")
        self.assertEqual(rows[0]["start_sample"], 8000)
        self.assertEqual(rows[0]["end_sample"], 24000)
        self.assertEqual(rows[0]["start_sec"], 0.5)
        self.assertEqual(rows[0]["end_sec"], 1.5)
        self.assertEqual(rows[0]["source_audio_id"], "source_audio_0000")
        self.assertEqual(rows[0]["backend"], COMMUNITY1_BACKEND)
        self.assertEqual(rows[1]["speaker_id"], "speaker_1")
        self.assertEqual(rows[1]["start_sample"], 32000)
        self.assertEqual(rows[1]["end_sample"], 52000)
        self.assertEqual([row["speaker_id"] for row in rows], ["speaker_0", "speaker_1"])
        again = annotation_to_speechcraft_regions(
            annotation,
            source_audio_id="source_audio_0000",
            analysis_audio_path="audio/analysis/source_audio_0000.mono16000.wav",
            sample_rate=16000,
            backend_version="4.0.3",
        )
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in again])


class LocalModelPathTests(unittest.TestCase):
    def test_missing_model_path_fails_clearly(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH", None)
            with self.assertRaisesRegex(RuntimeError, "local model path is missing"):
                resolve_community1_model_path({})

    def test_missing_snapshot_directory_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            missing = Path(temp_dir_raw) / "not-a-snapshot"
            with self.assertRaisesRegex(RuntimeError, "missing or not a directory"):
                resolve_community1_model_path({"diarization_model_path": str(missing)})

    def test_incomplete_snapshot_fails_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            snapshot = Path(temp_dir_raw) / "community-1"
            (snapshot / "embedding").mkdir(parents=True)
            (snapshot / "segmentation").mkdir()
            (snapshot / "embedding" / "pytorch_model.bin").write_bytes(b"too-small")
            (snapshot / "segmentation" / "pytorch_model.bin").write_bytes(b"too-small")
            ok, reason = community1_snapshot_ready(snapshot)
            self.assertFalse(ok)
            self.assertIn("incomplete", reason)
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                resolve_community1_model_path({"diarization_model_path": str(snapshot)})


class ProductionPathContractTests(unittest.TestCase):
    def test_production_diarization_does_not_import_nemo(self) -> None:
        import speechcraft_dataset.diarization as diarization
        import speechcraft_dataset.run as run

        diarization_source = inspect.getsource(diarization)
        run_source = inspect.getsource(run)
        self.assertNotIn("nemo", diarization_source.lower())
        self.assertNotIn("ClusteringDiarizer", diarization_source)
        self.assertNotIn("titanet", diarization_source.lower())
        self.assertNotIn("missing_nemo_dependency", run_source)
        self.assertNotIn("run_silero_vad", run_source)

    def test_live_dag_does_not_require_nemo_or_vad_stage(self) -> None:
        self.assertNotIn("vad", WORKER_STAGE_ORDER)
        self.assertEqual(WORKER_STAGE_ORDER[:3], ["source_audio", "audio_variants", "diarization"])
        self.assertEqual(
            failure_reason_codes("diarization", RuntimeError("pyannote.audio is unavailable: ModuleNotFoundError")),
            ["missing_pyannote_dependency"],
        )
        self.assertEqual(
            failure_reason_codes(
                "diarization",
                RuntimeError("pyannote Community-1 local model path is missing. Set diarization_model_path"),
            ),
            ["missing_diarization_model"],
        )


@unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, "requires worker audio deps")
class DiarizationModeTests(unittest.TestCase):
    def test_diarization_mode_does_not_run_external_vad(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            write_analysis_fixture(run_root)
            annotation = FakeAnnotation([(FakeTurn(0.1, 0.8), None, "SPEAKER_00")])
            with (
                patch("speechcraft_dataset.diarization.run_silero_vad") as vad,
                patch("speechcraft_dataset.diarization.resolve_community1_model_path", return_value=Path("/tmp/fake-community1")),
                patch("speechcraft_dataset.diarization.load_community1_pipeline", return_value=object()),
                patch("speechcraft_dataset.diarization.run_community1_pipeline", return_value=annotation),
                patch("speechcraft_dataset.diarization.pyannote_audio_version", return_value="4.0.3"),
                patch("speechcraft_dataset.diarization.write_speaker_samples", return_value=[]),
            ):
                summary = run_diarization(
                    run_root,
                    {"mode": "diarization", "diarization_model_path": "/tmp/fake-community1", "config_hash": "sha256:test"},
                )
            vad.assert_not_called()
            self.assertFalse((run_root / "artifacts" / "vad_segments.jsonl").exists())
            regions = read_jsonl(run_root / "artifacts" / "speaker_regions.jsonl")
            selection = read_json(run_root / "artifacts" / "speaker_selection.json")
            self.assertEqual(summary["backend"], COMMUNITY1_BACKEND)
            self.assertEqual(summary["reason_codes"], ["speaker_selection_required"])
            self.assertEqual(regions[0]["speaker_id"], "speaker_0")
            self.assertEqual(regions[0]["start_sample"], 1600)
            self.assertEqual(regions[0]["end_sample"], 12800)
            self.assertEqual(selection["available_speaker_ids"], ["speaker_0"])
            self.assertFalse(selection["selected"])

    def test_single_speaker_mode_still_uses_internal_silero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            write_analysis_fixture(run_root)

            def fake_vad(fake_run_root: Path, _config: dict) -> dict:
                (fake_run_root / "artifacts" / "vad_segments.jsonl").write_text(
                    json.dumps(
                        {
                            "id": "source_audio_0000_vad_000000",
                            "source_audio_id": "source_audio_0000",
                            "analysis_start_sample": 1600,
                            "analysis_end_sample": 12800,
                            "analysis_start_sec": 0.1,
                            "analysis_end_sec": 0.8,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return {"segment_count": 1}

            with (
                patch("speechcraft_dataset.diarization.run_silero_vad", side_effect=fake_vad) as vad,
                patch("speechcraft_dataset.diarization.write_speaker_samples", return_value=[]),
                patch("speechcraft_dataset.diarization.load_community1_pipeline") as load_pipeline,
            ):
                summary = run_diarization(run_root, {"mode": "single_speaker", "config_hash": "sha256:test"})
            vad.assert_called_once()
            load_pipeline.assert_not_called()
            regions = read_jsonl(run_root / "artifacts" / "speaker_regions.jsonl")
            selection = read_json(run_root / "artifacts" / "speaker_selection.json")
            self.assertEqual(summary["backend"], "single_speaker_vad_passthrough")
            self.assertEqual(regions[0]["speaker_id"], "speaker_0")
            self.assertTrue(selection["selected"])
            self.assertEqual(selection["target_speaker_id"], "speaker_0")

    def test_multi_file_concat_uses_one_pipeline_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            write_analysis_fixture(run_root, source_audio_id="source_audio_0000", duration_sec=1.0)
            write_analysis_fixture(run_root, source_audio_id="source_audio_0001", duration_sec=1.0)
            annotation = FakeAnnotation(
                [
                    (FakeTurn(0.1, 0.4), None, "SPEAKER_00"),
                    (FakeTurn(2.1, 2.4), None, "SPEAKER_00"),
                ]
            )
            with (
                patch("speechcraft_dataset.diarization.run_silero_vad") as vad,
                patch("speechcraft_dataset.diarization.resolve_community1_model_path", return_value=Path("/tmp/fake-community1")),
                patch("speechcraft_dataset.diarization.load_community1_pipeline", return_value=object()) as load_pipeline,
                patch("speechcraft_dataset.diarization.run_community1_pipeline", return_value=annotation) as infer,
                patch("speechcraft_dataset.diarization.pyannote_audio_version", return_value="4.0.3"),
                patch("speechcraft_dataset.diarization.write_speaker_samples", return_value=[]),
            ):
                summary = run_diarization(
                    run_root,
                    {
                        "mode": "diarization",
                        "diarization_concat_gap_sec": 1.0,
                        "diarization_model_path": "/tmp/fake-community1",
                    },
                )
            vad.assert_not_called()
            load_pipeline.assert_called_once()
            infer.assert_called_once()
            regions = read_jsonl(run_root / "artifacts" / "speaker_regions.jsonl")
            self.assertTrue(summary["multi_file"])
            self.assertEqual({row["source_audio_id"] for row in regions}, {"source_audio_0000", "source_audio_0001"})
            self.assertEqual({row["speaker_id"] for row in regions}, {"speaker_0"})
            self.assertEqual(regions[0]["start_sec"], 0.1)
            self.assertEqual(regions[1]["start_sec"], 0.1)


@unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, "requires worker audio deps")
class BufferContractTests(unittest.TestCase):
    def test_buffers_accept_pyannote_regions_without_vad_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            analysis = run_root / "audio" / "analysis" / "source_audio_0000.mono16000.wav"
            write_silent_wav(analysis, sample_rate=16000, duration_sec=2.0)
            artifacts = run_root / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "audio_variants_manifest.json").write_text(
                json.dumps(
                    {
                        "variants": [
                            {
                                "source_audio_id": "source_audio_0000",
                                "path": "audio/analysis/source_audio_0000.mono16000.wav",
                                "analysis_sample_rate": 16000,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (artifacts / "speaker_regions.jsonl").write_text(
                json.dumps(
                    {
                        "id": "speaker_0-source_audio_0000-1600-12800",
                        "source_audio_id": "source_audio_0000",
                        "speaker_id": "speaker_0",
                        "start_sample": 1600,
                        "end_sample": 12800,
                        "start_sec": 0.1,
                        "end_sec": 0.8,
                        "backend": COMMUNITY1_BACKEND,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (artifacts / "speaker_selection.json").write_text(
                json.dumps(
                    {
                        "mode": "diarization",
                        "selected": True,
                        "target_speaker_id": "speaker_0",
                        "source": "user",
                        "available_speaker_ids": ["speaker_0"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertFalse((artifacts / "vad_segments.jsonl").exists())
            summary = run_processing_buffers(run_root, {"analysis_sample_rate": 16000, "mode": "diarization"})
            buffers = json.loads((artifacts / "processing_buffers.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["buffer_count"], 1)
            self.assertNotIn("vad_segments_jsonl", summary["input_artifact_hashes"])
            self.assertEqual(buffers[0]["trusted_start_sample"], 1600)
            self.assertEqual(buffers[0]["trusted_end_sample"], 12800)
            self.assertEqual(buffers[0]["target_speaker_id"], "speaker_0")


@unittest.skipUnless(
    os.environ.get("SPEECHCRAFT_PYANNOTE_COMMUNITY1_SMOKE") == "1",
    "set SPEECHCRAFT_PYANNOTE_COMMUNITY1_SMOKE=1 to run local Community-1 inference",
)
class Community1LocalSmokeTests(unittest.TestCase):
    def test_local_snapshot_loads_offline(self) -> None:
        from speechcraft_dataset.diarization import load_community1_pipeline, pyannote_audio_dict, resolve_diarization_device

        path = resolve_community1_model_path({})
        pipeline = load_community1_pipeline(path, resolve_diarization_device({}))
        audio = pyannote_audio_dict([[0.0] * 16000], 16000)
        output = pipeline(audio)
        self.assertIsNotNone(getattr(output, "speaker_diarization", output))


if __name__ == "__main__":
    unittest.main()
