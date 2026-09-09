from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

from speechcraft_dataset.vr_slicer import (
    BOUNDARY_EPSILON_SEC,
    BufferScope,
    TRUSTED_GEOMETRY_FINGERPRINT,
    VR_O0_4,
    VrCutpoint,
    candidate_clip_weight,
    detect_cutpoints,
    generate_legal_candidates,
    geometry_fingerprint,
    schedule_candidates_by_buffer,
    slice_wav,
)


HAS_SLICE_DEPS = bool(
    importlib.util.find_spec("numpy")
    and importlib.util.find_spec("soundfile")
    and importlib.util.find_spec("onnxruntime")
    and importlib.util.find_spec("torch")
    and importlib.util.find_spec("silero_vad")
)

LAB_ROOT = Path("/home/aaravthegreat/Projects/buckeye-slicer-lab")
SPEAKER_TS_EVAL_SRC = Path("/home/aaravthegreat/Projects/speaker_ts_eval/src")


def _cut(*, buffer_id: str, time_sec: float, cut_id: str) -> VrCutpoint:
    return VrCutpoint(
        detector_name="vad_percentile_rms",
        source_id="source_a",
        recording_id="rec_a",
        buffer_id=buffer_id,
        cutpoint_id=cut_id,
        time_sec=time_sec,
        interval_start_sec=time_sec - 0.04,
        interval_end_sec=time_sec + 0.04,
        score=1.0,
    )


def _frame(*, center: float, speech_prob: float, rms_dbfs: float, hop: float = 0.032) -> dict:
    half = hop / 2.0
    return {
        "window_start_sec": round(center - half, 6),
        "window_end_sec": round(center + half, 6),
        "center_sec": round(center, 6),
        "speech_prob": speech_prob,
        "rms_dbfs": rms_dbfs,
        "voicing_score": 0.8 if speech_prob >= 0.5 else 0.1,
        "offset_samples": 0,
    }


class VrSlicerContractTests(unittest.TestCase):
    def test_geometry_fingerprint_matches_locked_o0_4(self) -> None:
        self.assertEqual(geometry_fingerprint(VR_O0_4), TRUSTED_GEOMETRY_FINGERPRINT)
        self.assertEqual(VR_O0_4.name, "O0_4")
        self.assertEqual(VR_O0_4.window_samples, 512)
        self.assertEqual(VR_O0_4.hop_samples, 512)
        self.assertEqual(VR_O0_4.offsets, (0, 64, 128, 192, 256, 320, 384, 448))
        self.assertEqual(VR_O0_4.sample_rate_hz, 16000)
        self.assertEqual(VR_O0_4.vad_backend, "silero_official_onnx")
        self.assertEqual(VR_O0_4.vad_threshold, 0.2)
        self.assertEqual(VR_O0_4.vad_min_run_ms, 40.0)
        self.assertEqual(VR_O0_4.percentile_rms_percentile, 10.0)
        self.assertEqual(VR_O0_4.percentile_rms_margin_db, 3.0)
        self.assertEqual(VR_O0_4.min_clip_sec, 3.0)
        self.assertEqual(VR_O0_4.preferred_min_sec, 6.0)
        self.assertEqual(VR_O0_4.preferred_max_sec, 8.0)
        self.assertEqual(VR_O0_4.target_clip_sec, 8.0)
        self.assertEqual(VR_O0_4.max_clip_sec, 15.0)

    def test_production_module_does_not_import_asr_or_mfa(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "speechcraft_dataset" / "vr_slicer.py"
        ).read_text(encoding="utf-8")
        lowered = source.lower()
        self.assertNotIn("faster_whisper", lowered)
        self.assertNotIn("montreal", lowered)
        self.assertNotIn("praatio", lowered)
        self.assertNotIn("textgrid", lowered)
        self.assertNotIn("mfa", lowered)
        self.assertNotIn("whisper", lowered)
        production_body = source.split("from __future__", 1)[-1]
        self.assertNotIn("adapter.config", production_body)
        self.assertNotIn("speaker_ts_eval", production_body)
        self.assertNotIn("faster_whisper", production_body)

    def test_short_speech_region_emits_no_legal_clips(self) -> None:
        cuts = [_cut(buffer_id="buffer_000000", time_sec=0.4, cut_id="c0"), _cut(buffer_id="buffer_000000", time_sec=2.1, cut_id="c1")]
        legal = generate_legal_candidates(cuts)
        self.assertEqual(legal, [])

    def test_clip_min_and_max_duration_rules(self) -> None:
        cuts = [
            _cut(buffer_id="buffer_000000", time_sec=1.0, cut_id="c0"),
            _cut(buffer_id="buffer_000000", time_sec=3.5, cut_id="c1"),
            _cut(buffer_id="buffer_000000", time_sec=9.0, cut_id="c2"),
            _cut(buffer_id="buffer_000000", time_sec=17.5, cut_id="c3"),
        ]
        legal = generate_legal_candidates(cuts)
        spans = {(row.start_sec, row.end_sec) for row in legal}
        self.assertIn((1.0, 9.0), spans)
        self.assertNotIn((1.0, 3.5), spans)
        self.assertNotIn((1.0, 17.5), spans)
        self.assertTrue(all(3.0 - BOUNDARY_EPSILON_SEC <= row.duration_sec <= 15.0 + BOUNDARY_EPSILON_SEC for row in legal))

    def test_preferred_duration_outranks_long_legal_span(self) -> None:
        cuts = [
            _cut(buffer_id="buffer_000000", time_sec=0.5, cut_id="c0"),
            _cut(buffer_id="buffer_000000", time_sec=8.5, cut_id="c1"),
            _cut(buffer_id="buffer_000000", time_sec=15.0, cut_id="c2"),
        ]
        selected = schedule_candidates_by_buffer(generate_legal_candidates(cuts))
        spans = {(row.start_sec, row.end_sec) for row in selected}
        self.assertIn((0.5, 8.5), spans)
        self.assertNotIn((0.5, 15.0), spans)
        self.assertGreater(
            candidate_clip_weight(duration_sec=8.0, geometry=VR_O0_4),
            candidate_clip_weight(duration_sec=14.5, geometry=VR_O0_4),
        )

    def test_stable_candidate_ordering_across_buffers(self) -> None:
        cuts = [
            _cut(buffer_id="buffer_000001", time_sec=20.0, cut_id="b1-0"),
            _cut(buffer_id="buffer_000001", time_sec=28.0, cut_id="b1-1"),
            _cut(buffer_id="buffer_000000", time_sec=1.0, cut_id="b0-0"),
            _cut(buffer_id="buffer_000000", time_sec=9.0, cut_id="b0-1"),
        ]
        selected = schedule_candidates_by_buffer(generate_legal_candidates(cuts))
        self.assertEqual([row.buffer_id for row in selected], ["buffer_000000", "buffer_000001"])
        again = schedule_candidates_by_buffer(generate_legal_candidates(cuts))
        self.assertEqual(
            [(row.buffer_id, row.start_sec, row.end_sec) for row in selected],
            [(row.buffer_id, row.start_sec, row.end_sec) for row in again],
        )

    def test_rms_valley_is_selected_inside_silence_run(self) -> None:
        frames = []
        for index in range(200):
            center = 0.1 + index * 0.032
            in_valley = 3.9 <= center <= 4.2
            frames.append(
                _frame(
                    center=center,
                    speech_prob=0.04 if in_valley else 0.92,
                    rms_dbfs=-55.0 if in_valley else -18.0,
                )
            )
        cuts = detect_cutpoints(
            frames=frames,
            buffers=[BufferScope("buffer_000000", 0.0, 8.0)],
            source_id="source_a",
            recording_id="rec_a",
        )
        self.assertGreaterEqual(len(cuts), 1)
        best = min(cuts, key=lambda cut: abs(cut.time_sec - 4.05))
        self.assertLess(abs(best.time_sec - 4.05), 0.08)
        self.assertLess(best.metadata["rms_min_dbfs"], -40.0)


@unittest.skipUnless(HAS_SLICE_DEPS, "requires numpy/soundfile/onnxruntime")
class VrSlicerAudioTests(unittest.TestCase):
    def _write_wav(self, path: Path, samples) -> None:
        import soundfile as sf

        sf.write(str(path), samples, 16000, subtype="PCM_16")

    def _speech_like(self, duration_sec: float, *, amplitude: float = 0.2):
        import numpy as np

        n = int(round(duration_sec * 16000))
        t = np.arange(n, dtype=np.float32) / 16000.0
        voiced = (
            np.sin(2 * np.pi * 120.0 * t)
            + 0.45 * np.sin(2 * np.pi * 240.0 * t)
            + 0.25 * np.sin(2 * np.pi * 360.0 * t)
        )
        noise = np.random.default_rng(7).normal(0.0, 0.03, size=n).astype(np.float32)
        return (amplitude * voiced + noise).astype(np.float32)

    def _pattern(self, parts: list[tuple[str, float]]):
        import numpy as np

        chunks = []
        for kind, duration in parts:
            if kind == "speech":
                chunks.append(self._speech_like(duration))
            else:
                chunks.append(np.zeros(int(round(duration * 16000)), dtype=np.float32))
        return np.concatenate(chunks)

    def test_short_wav_can_legally_emit_zero_clips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            wav = Path(temp_dir_raw) / "short.wav"
            self._write_wav(wav, self._speech_like(1.5))
            result = slice_wav(
                wav,
                recording_id="short",
                sample_rate_hz=16000,
                buffers=[BufferScope("buffer_000000", 0.0, 1.5)],
            )
            self.assertEqual(result.geometry_fingerprint, TRUSTED_GEOMETRY_FINGERPRINT)
            self.assertEqual(result.clips, ())

    def test_deterministic_slicing_for_fixed_input(self) -> None:
        samples = self._pattern(
            [
                ("speech", 2.0),
                ("silence", 0.5),
                ("speech", 8.0),
                ("silence", 0.5),
                ("speech", 8.0),
                ("silence", 0.5),
                ("speech", 2.0),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            wav = Path(temp_dir_raw) / "long.wav"
            self._write_wav(wav, samples)
            duration = len(samples) / 16000.0
            first = slice_wav(
                wav,
                recording_id="long",
                sample_rate_hz=16000,
                buffers=[BufferScope("buffer_000000", 0.0, duration)],
            )
            second = slice_wav(
                wav,
                recording_id="long",
                sample_rate_hz=16000,
                buffers=[BufferScope("buffer_000000", 0.0, duration)],
            )
            self.assertEqual(
                [(clip.start_sec, clip.end_sec, clip.clip_id) for clip in first.clips],
                [(clip.start_sec, clip.end_sec, clip.clip_id) for clip in second.clips],
            )
            for clip in first.clips:
                self.assertGreaterEqual(clip.duration_sec, 3.0 - BOUNDARY_EPSILON_SEC)
                self.assertLessEqual(clip.duration_sec, 15.0 + BOUNDARY_EPSILON_SEC)

    def test_silence_boundaries_create_cut_to_cut_clips(self) -> None:
        samples = self._pattern(
            [
                ("speech", 2.0),
                ("silence", 0.6),
                ("speech", 8.0),
                ("silence", 0.6),
                ("speech", 2.0),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            wav = Path(temp_dir_raw) / "bounded.wav"
            self._write_wav(wav, samples)
            duration = len(samples) / 16000.0
            result = slice_wav(
                wav,
                recording_id="bounded",
                sample_rate_hz=16000,
                buffers=[BufferScope("buffer_000000", 0.0, duration)],
            )
            for clip in result.clips:
                self.assertGreater(clip.start_sec, 0.05)
                self.assertLess(clip.end_sec, duration - 0.05)

    def test_oracle_matches_authoritative_o0_4_within_experiment_tolerance(self) -> None:
        if not LAB_ROOT.is_dir() or not SPEAKER_TS_EVAL_SRC.is_dir():
            self.skipTest("buckeye-slicer-lab / speaker_ts_eval not present for oracle comparison")
        samples = self._pattern(
            [
                ("speech", 2.0),
                ("silence", 0.5),
                ("speech", 8.0),
                ("silence", 0.5),
                ("speech", 8.0),
                ("silence", 0.5),
                ("speech", 2.0),
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            wav = Path(temp_dir_raw) / "oracle.wav"
            self._write_wav(wav, samples)
            duration = len(samples) / 16000.0
            local = slice_wav(
                wav,
                recording_id="oracle",
                sample_rate_hz=16000,
                buffers=[BufferScope("full", 0.0, duration)],
            )
            os.environ["SPEAKER_TS_EVAL_SRC"] = str(SPEAKER_TS_EVAL_SRC)
            previous_path = list(sys.path)
            sys.path.insert(0, str(LAB_ROOT))
            try:
                try:
                    from adapter.config import O0_4 as VR_SLICER
                    from adapter.diagnostics import geometry_fingerprint as lab_fingerprint
                    from adapter.run import run_slicer
                    from adapter.types import SlicerRequest
                    from referee.types import BufferScope as LabBufferScope
                except Exception as exc:
                    self.skipTest(f"authoritative VR adapter could not be imported: {exc}")
                lab_fp = lab_fingerprint(VR_SLICER)
                self.assertEqual(lab_fp, TRUSTED_GEOMETRY_FINGERPRINT)
                request = SlicerRequest(
                    recording_id="oracle",
                    audio_path=wav,
                    sample_rate_hz=16000,
                    buffers=(LabBufferScope("full", 0.0, float(duration)),),
                    config=VR_SLICER,
                )
                authoritative = run_slicer(request)
            finally:
                sys.path[:] = previous_path
            local_bounds = [(round(clip.start_sec, 6), round(clip.end_sec, 6)) for clip in local.clips]
            lab_bounds = [(round(clip.start_sec, 6), round(clip.end_sec, 6)) for clip in authoritative.clips]
            if len(local_bounds) != len(lab_bounds):
                self.fail(
                    "VR production slicer drifted from authoritative O0_4 clip count: "
                    f"local={local_bounds} lab={lab_bounds}"
                )
            mismatches = []
            for index, (left, right) in enumerate(zip(local_bounds, lab_bounds)):
                if abs(left[0] - right[0]) > BOUNDARY_EPSILON_SEC or abs(left[1] - right[1]) > BOUNDARY_EPSILON_SEC:
                    mismatches.append((index, left, right))
            if mismatches:
                self.fail(
                    "VR production slicer drifted from authoritative O0_4 boundaries "
                    f"(tolerance {BOUNDARY_EPSILON_SEC}s): {mismatches}"
                )


if __name__ == "__main__":
    unittest.main()
