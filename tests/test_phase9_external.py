"""Phase 9 external baselines: buffer mapping, adapters, O0_4 freeze, FFmpeg parse."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from adapter.config import O0_4, O0_2, A_O50_8
from adapter.diagnostics import geometry_fingerprint
from adapter.external_baselines import (
    ADAPTER_VERSION,
    FFMPEG_COMMON,
    FFMPEG_NATIVE,
    LIBROSA_COMMON,
    OPENVPI_COMMON,
    OPENVPI_NATIVE,
    OPENVPI_PARAMS,
    OPENVPI_SLICER2_SHA256,
    PHASE9_EXTERNAL_SPECS,
    PHASE9_SMOKE_SYSTEMS,
    PYDUB_COMMON,
    TRUSTED_O0_4_GEOMETRY_FINGERPRINT,
    hash_file_sha256,
    openvpi_slicer2_path,
    require_frozen_o0_4,
    require_openvpi_vendor,
)
from adapter.external_detect import (
    ExternalAudioRequest,
    extract_buffer_audio,
    map_local_to_recording,
)
from adapter.external_execute import classify_native_clips
from adapter.external_ffmpeg import FFmpegSilenceError, parse_silencedetect_stderr
from referee.types import BufferScope, Clip, Cutpoint, SlicerResult


def _has_module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


needs_numpy = pytest.mark.skipif(not _has_module("numpy"), reason="numpy missing")
needs_librosa = pytest.mark.skipif(not _has_module("librosa"), reason="librosa missing")
needs_pydub = pytest.mark.skipif(not _has_module("pydub"), reason="pydub missing")
needs_soundfile = pytest.mark.skipif(not _has_module("soundfile"), reason="soundfile missing")
needs_ffmpeg = pytest.mark.skipif(
    not Path("/usr/bin/ffmpeg").is_file(), reason="system FFmpeg missing"
)


FFMPEG_CAPTURED_STDERR = """\
[aist#0:0/pcm_s16le @ 0x563393cc5880] Guessed Channel Layout: mono
Input #0, wav, from '/tmp/p9_sil_test.wav':
  Duration: 00:00:02.80, bitrate: 256 kb/s
  Stream #0:0: Audio: pcm_s16le ([1][0][0][0] / 0x0001), 16000 Hz, mono, s16, 256 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (pcm_s16le (native) -> pcm_s16le (native))
Output #0, null, to 'pipe:':
[Parsed_silencedetect_0 @ 0x7f0b54003d40] silence_start: 1
[Parsed_silencedetect_0 @ 0x7f0b54003d40] silence_end: 1.800063 | silence_duration: 0.800063
[out#0/null @ 0x563393cce5c0] video:0KiB audio:88KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
"""


class TestFrozenO04:
    def test_o0_4_fingerprint_matches_phase7_8(self) -> None:
        assert geometry_fingerprint(O0_4) == TRUSTED_O0_4_GEOMETRY_FINGERPRINT
        assert require_frozen_o0_4() is O0_4

    def test_phase9_does_not_retune_o0_4(self) -> None:
        assert O0_4.window_samples == 512
        assert O0_4.hop_samples == 512
        assert O0_4.offsets == (0, 64, 128, 192, 256, 320, 384, 448)
        assert O0_4.sample_rate_hz == 16000
        assert O0_4.vad_backend == "silero_official_onnx"
        assert O0_4.vad_threshold == 0.2
        assert O0_4.rms_policy is None

    def test_main_comparison_excludes_internal_family(self) -> None:
        assert "A_O50_8" not in PHASE9_SMOKE_SYSTEMS
        assert "O25_4" not in PHASE9_SMOKE_SYSTEMS
        assert "O0_2" not in PHASE9_SMOKE_SYSTEMS
        assert geometry_fingerprint(A_O50_8) != geometry_fingerprint(O0_4)
        assert geometry_fingerprint(O0_2) != geometry_fingerprint(O0_4)


class TestVendorAndIdentity:
    def test_openvpi_vendor_hash_and_license(self) -> None:
        vendor = require_openvpi_vendor()
        assert vendor["sha256"] == OPENVPI_SLICER2_SHA256
        assert hash_file_sha256(openvpi_slicer2_path()) == OPENVPI_SLICER2_SHA256
        source = openvpi_slicer2_path().read_text(encoding="utf-8")
        assert "class Slicer" in source
        assert "min_length: int = 5000" in source

    def test_openvpi_params_are_cli_defaults(self) -> None:
        assert OPENVPI_PARAMS["threshold_db"] == -40.0
        assert OPENVPI_PARAMS["min_length_ms"] == 5000
        assert OPENVPI_PARAMS["min_interval_ms"] == 300
        assert OPENVPI_PARAMS["hop_size_ms"] == 10
        assert OPENVPI_PARAMS["max_sil_kept_ms"] == 500

    def test_spec_fingerprints_stable_and_distinct(self) -> None:
        fingerprints = [spec.fingerprint() for spec in PHASE9_EXTERNAL_SPECS]
        assert len(set(fingerprints)) == len(fingerprints)
        assert OPENVPI_NATIVE.fingerprint() == OPENVPI_NATIVE.fingerprint()
        assert OPENVPI_NATIVE.mode == "native"
        assert OPENVPI_COMMON.packing == "buckeye_common_optimal_3_15_packer"
        assert ADAPTER_VERSION == "phase9_external_v1"
        assert FFMPEG_NATIVE.params["filter"] == "silencedetect=noise=-60dB:duration=2"
        assert FFMPEG_COMMON.candidate_rule.startswith("midpoint")

    def test_rvc_comparison_note_exists(self) -> None:
        note = Path(__file__).resolve().parents[1] / "RVC_SLICER_COMPARISON.md"
        text = note.read_text(encoding="utf-8")
        assert "not counted as an independent competitor" in text
        assert "algorithmically equivalent" in text


class TestBufferMapping:
    def test_local_time_maps_to_recording_time(self) -> None:
        assert map_local_to_recording(3.0, 10.0) == 13.0

    def test_float_mapping_is_clamped_to_buffer(self) -> None:
        from adapter.external_detect import clamp_recording_time

        buf = BufferScope("buf0", 224.4, 228.432)
        assert clamp_recording_time(228.43200000000002, buf) == 228.432
        assert clamp_recording_time(224.4, buf) == 224.4

    def test_request_firewall_fields(self) -> None:
        request = ExternalAudioRequest(
            recording_id="rec0",
            sample_rate_hz=16000,
            buffers=(BufferScope("buf0", 10.0, 20.0),),
            audio=None,
            spec=LIBROSA_COMMON,
        )
        request.assert_firewall()
        assert {item.name for item in fields(request)} == {
            "recording_id",
            "sample_rate_hz",
            "buffers",
            "audio",
            "spec",
        }


class TestFFmpegParser:
    def test_parser_uses_captured_stderr(self) -> None:
        intervals = parse_silencedetect_stderr(FFMPEG_CAPTURED_STDERR, duration_sec=2.8)
        assert intervals == [(1.0, 1.800063)]

    def test_trailing_silence_closes_at_duration(self) -> None:
        text = "[silencedetect @ x] silence_start: 4.5\n"
        assert parse_silencedetect_stderr(text, duration_sec=6.0) == [(4.5, 6.0)]

    def test_end_without_start_fails(self) -> None:
        with pytest.raises(FFmpegSilenceError, match="without silence_start"):
            parse_silencedetect_stderr(
                "[silencedetect @ x] silence_end: 1.0 | silence_duration: 1.0\n",
                duration_sec=2.0,
            )

    def test_malformed_start_fails(self) -> None:
        with pytest.raises(FFmpegSilenceError, match="malformed"):
            parse_silencedetect_stderr(
                "[silencedetect @ x] silence_start: not-a-number\n",
                duration_sec=2.0,
            )


class TestNativeLegality:
    def test_does_not_truncate_or_merge(self) -> None:
        raw = (
            ("rec", "buf", 0.0, 2.0),
            ("rec", "buf", 2.0, 20.0),
            ("rec", "buf", 0.0, 6.0),
        )
        legal, stats = classify_native_clips(raw)
        assert legal == (("rec", "buf", 0.0, 6.0),)
        assert stats.clips_lt_3 == 1
        assert stats.clips_gt_15 == 1
        assert stats.legal_clip_count == 1
        assert stats.native_compatible is True

    def test_all_illegal_is_not_benchmark_compatible(self) -> None:
        raw = (("rec", "buf", 0.0, 1.0), ("rec", "buf", 0.0, 20.0))
        legal, stats = classify_native_clips(raw)
        assert legal == ()
        assert stats.native_compatible is False
        assert "not directly benchmark-compatible" in stats.note


class TestNativeWallCutpoints:
    def test_buffer_wall_clip_edges_are_not_public_cutpoints(self) -> None:
        from adapter.external_execute import _result_from_clips

        clips = (("rec0", "buf0", 10.0, 16.0),)
        result = _result_from_clips(
            clips,
            buffer_bounds={("rec0", "buf0"): (10.0, 20.0)},
        )
        assert result.clips == (Clip("rec0", "buf0", 10.0, 16.0),)
        assert result.cutpoints == (Cutpoint("rec0", "buf0", 16.0),)


def _tone_silence_tone(
    *,
    sample_rate: int = 16000,
    tone_sec: float = 1.0,
    silence_sec: float = 0.8,
    extra_head: float = 0.0,
    extra_tail: float = 0.0,
):
    import numpy as np

    head = np.zeros(int(extra_head * sample_rate), dtype=np.float32)
    t = np.linspace(0, tone_sec, int(tone_sec * sample_rate), endpoint=False)
    tone = (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
    silence = np.zeros(int(silence_sec * sample_rate), dtype=np.float32)
    tail = np.zeros(int(extra_tail * sample_rate), dtype=np.float32)
    return np.concatenate([head, tone, silence, tone, tail]), sample_rate


@needs_numpy
@needs_librosa
class TestScopeIsolation:
    def test_outside_buffer_audio_does_not_change_inside_result(self) -> None:
        import numpy as np

        from adapter.external_detect import detect_librosa_local

        sr = 16000
        audio = np.zeros(sr * 4, dtype=np.float32)
        t = np.linspace(0, 1.0, sr, endpoint=False)
        audio[sr : 2 * sr] = (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
        buffer = BufferScope("buf0", 1.0, 2.0)
        inside = extract_buffer_audio(audio, sr, buffer)
        left = detect_librosa_local(inside, sr, LIBROSA_COMMON.params)
        mutated = audio.copy()
        mutated[:sr] = 0.9
        mutated[3 * sr :] = 0.9
        inside2 = extract_buffer_audio(mutated, sr, buffer)
        right = detect_librosa_local(inside2, sr, LIBROSA_COMMON.params)
        assert left.native_clips_local == right.native_clips_local
        assert left.candidate_times_local == right.candidate_times_local
        np.testing.assert_array_equal(inside, inside2)


@needs_numpy
@needs_librosa
class TestLibrosaAdapter:
    def test_tone_silence_tone_boundaries(self) -> None:
        from adapter.external_detect import detect_librosa_local

        samples, sr = _tone_silence_tone()
        result = detect_librosa_local(samples, sr, LIBROSA_COMMON.params)
        assert len(result.native_clips_local) == 2
        first_end = result.native_clips_local[0][1]
        second_start = result.native_clips_local[1][0]
        assert 0.9 < first_end < 1.2
        assert 1.6 < second_start < 2.0
        mapped = [map_local_to_recording(t, 10.0) for t in result.candidate_times_local]
        assert mapped
        assert all(10.0 < t < 20.0 for t in mapped)


@needs_numpy
@needs_pydub
class TestPydubAdapter:
    def test_tone_silence_tone_midpoint(self) -> None:
        from adapter.external_detect import detect_pydub_local

        samples, sr = _tone_silence_tone(tone_sec=1.2, silence_sec=1.2)
        result = detect_pydub_local(samples, sr, PYDUB_COMMON.params)
        assert result.candidate_times_local
        mid = result.candidate_times_local[0]
        assert 1.2 < mid < 2.2


@needs_numpy
class TestOpenVPIAdapter:
    def test_timestamp_conversion(self) -> None:
        from adapter.external_detect import detect_openvpi_local

        samples, sr = _tone_silence_tone(tone_sec=6.0, silence_sec=1.0)
        result = detect_openvpi_local(samples, sr, OPENVPI_NATIVE.params)
        assert result.native_clips_local
        for start, end in result.native_clips_local:
            assert 0.0 <= start < end <= 13.0 + 1e-6
        mapped = map_local_to_recording(3.0, 10.0)
        assert mapped == 13.0
        if result.candidate_times_local:
            rec = map_local_to_recording(result.candidate_times_local[0], 10.0)
            assert 10.0 < rec < 23.0


class TestCommonPacker:
    def test_candidates_pack_to_legal_clips_without_moving_times(self) -> None:
        from adapter.external_detect import BufferDetection
        from adapter.external_execute import pack_common

        try:
            detections = (
                BufferDetection(
                    buffer_id="buf0",
                    recording_id="rec0",
                    buffer_start_sec=0.0,
                    buffer_end_sec=20.0,
                    native_clips=(),
                    candidate_times=(1.25, 8.5, 14.0),
                    detector_runtime_sec=0.0,
                ),
            )
            result, candidates, selected = pack_common(detections, OPENVPI_COMMON)
        except RuntimeError as exc:
            if "Canonical slicer path unavailable" in str(exc):
                pytest.skip("speaker_ts_eval unavailable")
            raise
        assert {item.name for item in fields(result)} == {"cutpoints", "clips"}
        assert result.clips
        packed_times = sorted({cut.time_sec for cut in candidates})
        assert packed_times == [1.25, 8.5, 14.0]
        for clip in result.clips:
            assert 3.0 <= clip.end_sec - clip.start_sec <= 15.0
            assert clip.start_sec in packed_times
            assert clip.end_sec in packed_times
        assert selected


class TestNeutralEvaluatorPath:
    def test_external_result_is_slicer_result(self) -> None:
        legal, _stats = classify_native_clips((("rec0", "buf0", 1.0, 8.0),))
        from adapter.external_execute import _result_from_clips

        result = _result_from_clips(legal)
        assert isinstance(result, SlicerResult)
        assert {item.name for item in fields(result)} == {"cutpoints", "clips"}


@needs_numpy
@needs_ffmpeg
@needs_soundfile
class TestFFmpegIntegration:
    def test_real_ffmpeg_on_tiny_wav(self, tmp_path: Path) -> None:
        import soundfile as sf

        from adapter.external_ffmpeg import run_silencedetect_on_wav

        samples, sr = _tone_silence_tone(tone_sec=1.0, silence_sec=0.8)
        wav = tmp_path / "tiny.wav"
        sf.write(str(wav), samples, sr, subtype="PCM_16")
        intervals, stderr = run_silencedetect_on_wav(
            wav,
            duration_sec=len(samples) / sr,
            filter_arg="silencedetect=noise=-60dB:duration=0.3",
        )
        assert "silence_start" in stderr
        assert len(intervals) == 1
        start, end = intervals[0]
        assert 0.8 < start < 1.2
        assert 1.6 < end < 2.1


def test_full_cohort_runner_refuses_without_confirm() -> None:
    from adapter.external_validate import run_phase9_full

    with pytest.raises(RuntimeError, match="refusing to run the 120-recording"):
        run_phase9_full(output_dir=Path("/tmp/phase9_full_should_not_exist"))
