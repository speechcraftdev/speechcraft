from __future__ import annotations
import json
import importlib.util
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from speechcraft_dataset.assembly import assemble_candidate_review_clips
from speechcraft_dataset.buffers import read_analysis_audio, run_processing_buffers
from speechcraft_dataset.export import export_native_candidate_clips
from speechcraft_dataset.generate_qc_scores import main as generate_qc_scores_main
from speechcraft_dataset.io import sha256_file
from speechcraft_dataset.models import check_asr_model
from speechcraft_dataset.rerun_slicer import main as rerun_slicer_main
from speechcraft_dataset.run import WORKER_STAGE_ORDER, main
from speechcraft_dataset.vr_slicer import VrPackedClip, VrSlicerResult, TRUSTED_GEOMETRY_FINGERPRINT
import sys
HAS_WORKER_AUDIO_DEPS = bool(importlib.util.find_spec('numpy') and importlib.util.find_spec('soundfile'))

def write_silent_wav(path: Path, *, sample_rate: int=16000, duration_sec: float=0.1) -> None:
    frames = int(sample_rate * duration_sec)
    with wave.open(str(path), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b'\x00\x00' * frames)

def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))

def write_minimal_native_export_fixture(run_root: Path, temp_dir: Path, candidates: list[dict], *, dataset_qc: dict | None=None) -> Path:
    artifacts = run_root / 'artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    source = temp_dir / 'source_48k.wav'
    write_silent_wav(source, sample_rate=48000, duration_sec=5.0)
    (artifacts / 'source_audio_manifest.json').write_text(json.dumps({'sources': [{'source_audio_id': 'source_audio_0000', 'path': str(source), 'sample_rate': 48000, 'num_channels': 1, 'sample_width_bytes': 2, 'num_samples': 240000, 'duration_sec': 5.0}]}), encoding='utf-8')
    (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': [{'source_audio_id': 'source_audio_0000', 'kind': 'analysis_audio', 'source_sample_rate': 48000, 'analysis_sample_rate': 16000, 'source_num_samples': 240000, 'analysis_num_samples': 80000}]}), encoding='utf-8')
    (artifacts / 'candidate_review_manifest.json').write_text(json.dumps(candidates), encoding='utf-8')
    if dataset_qc is not None:
        (artifacts / 'dataset_qc.json').write_text(json.dumps(dataset_qc), encoding='utf-8')
    return artifacts

class DatasetWorkerRunCliTests(unittest.TestCase):

    def test_slicer_rerun_runs_candidate_assembly_then_qc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            run_root.mkdir()
            config_path = run_root / 'input_config.json'
            config_path.write_text(json.dumps({'cutpoint_min_gap_ms': 40}), encoding='utf-8')
            with patch('speechcraft_dataset.rerun_slicer.assemble_candidate_review_clips_locked', return_value={'candidate_review_clips': 2}) as assembly, patch('speechcraft_dataset.rerun_slicer.run_transcript_qc_stage', return_value={'clip_count': 2, 'scored_count': 2}) as transcript_qc, patch('speechcraft_dataset.rerun_slicer.run_speaker_purity_stage', return_value={'clip_count': 2, 'scored_count': 2}) as speaker_purity:
                exit_code = rerun_slicer_main(['--run-root', str(run_root), '--config', str(config_path)])
            self.assertEqual(exit_code, 0)
            assembly.assert_called_once()
            transcript_qc.assert_called_once()
            speaker_purity.assert_called_once()
            status = read_json(run_root / 'status.json')
            self.assertTrue(status['ok'])
            self.assertEqual(status['stage'], 'speaker_purity')
            self.assertEqual(status['summary']['clip_count'], 2)
            self.assertTrue(read_json(run_root / 'config.json')['config_hash'])

    def test_slicer_rerun_keeps_candidate_clips_when_qc_artifact_generation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            run_root.mkdir()
            config_path = run_root / 'input_config.json'
            config_path.write_text(json.dumps({'cutpoint_min_gap_ms': 40}), encoding='utf-8')
            with patch('speechcraft_dataset.rerun_slicer.assemble_candidate_review_clips_locked', return_value={'candidate_review_clips': 2}), patch('speechcraft_dataset.rerun_slicer.run_transcript_qc_stage', return_value={'clip_count': 2, 'scored_count': 2}), patch('speechcraft_dataset.rerun_slicer.run_speaker_purity_stage', side_effect=RuntimeError('speaker model unavailable')):
                exit_code = rerun_slicer_main(['--run-root', str(run_root), '--config', str(config_path)])
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            self.assertTrue(status['ok'])
            self.assertEqual(status['stage'], 'candidate_review_clips')
            self.assertEqual(status['summary']['candidate_review_clips'], 2)
            self.assertEqual(status['reason_codes'], ['qc_artifacts_failed'])

    def test_slicer_rerun_clears_stale_downstream_qc_artifacts_before_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            config_path = run_root / 'input_config.json'
            config_path.write_text(json.dumps({'cutpoint_min_gap_ms': 40}), encoding='utf-8')
            (artifacts / 'speaker_purity.json').write_text('{}', encoding='utf-8')
            (artifacts / 'dataset_qc.json').write_text('{}', encoding='utf-8')
            with patch('speechcraft_dataset.rerun_slicer.assemble_candidate_review_clips_locked', return_value={'candidate_review_clips': 2}), patch('speechcraft_dataset.rerun_slicer.run_transcript_qc_stage', return_value={'clip_count': 2, 'scored_count': 2}), patch('speechcraft_dataset.rerun_slicer.run_speaker_purity_stage', side_effect=RuntimeError('speaker model unavailable')):
                exit_code = rerun_slicer_main(['--run-root', str(run_root), '--config', str(config_path)])
            self.assertEqual(exit_code, 0)
            self.assertFalse((artifacts / 'speaker_purity.json').exists())
            self.assertFalse((artifacts / 'dataset_qc.json').exists())

    def test_generate_qc_scores_clears_stale_speaker_artifact_when_speaker_stage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            config_path = run_root / 'config.json'
            config_path.write_text(json.dumps({}), encoding='utf-8')
            (artifacts / 'candidate_review_manifest.json').write_text('[]', encoding='utf-8')
            (artifacts / 'speaker_selection.json').write_text(json.dumps({'target_speaker_id': 'speaker_0'}), encoding='utf-8')
            (artifacts / 'speaker_regions.jsonl').write_text('', encoding='utf-8')
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': []}), encoding='utf-8')
            (artifacts / 'speaker_purity.json').write_text('{}', encoding='utf-8')
            with patch('speechcraft_dataset.generate_qc_scores.run_transcript_qc_stage', return_value={'clip_count': 0, 'scored_count': 0}), patch('speechcraft_dataset.generate_qc_scores.run_speaker_purity_stage', side_effect=RuntimeError('speaker model unavailable')):
                exit_code = generate_qc_scores_main(['--run-root', str(run_root), '--config', str(config_path)])
            self.assertEqual(exit_code, 1)
            self.assertFalse((artifacts / 'speaker_purity.json').exists())
            status = read_json(run_root / 'status.json')
            self.assertFalse(status['ok'])
            self.assertEqual(status['reason_codes'], ['dataset_qc_score_generation_failed'])

    def test_generate_qc_scores_refuses_finalized_qc_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            config_path = run_root / 'config.json'
            config_path.write_text(json.dumps({}), encoding='utf-8')
            (artifacts / 'candidate_review_manifest.json').write_text('[]', encoding='utf-8')
            (artifacts / 'speaker_selection.json').write_text(json.dumps({'target_speaker_id': 'speaker_0'}), encoding='utf-8')
            (artifacts / 'speaker_regions.jsonl').write_text('', encoding='utf-8')
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': []}), encoding='utf-8')
            (artifacts / 'dataset_qc.json').write_text('{}', encoding='utf-8')
            exit_code = generate_qc_scores_main(['--run-root', str(run_root), '--config', str(config_path)])
            self.assertEqual(exit_code, 1)
            self.assertTrue((artifacts / 'dataset_qc.json').exists())
            status = read_json(run_root / 'status.json')
            self.assertFalse(status['ok'])
            self.assertIn('dataset_qc_already_finalized', status['error'])

    def test_generate_qc_scores_clears_export_and_dataset_qc_artifacts_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw) / 'run'
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            config_path = run_root / 'config.json'
            config_path.write_text(json.dumps({}), encoding='utf-8')
            (artifacts / 'candidate_review_manifest.json').write_text('[]', encoding='utf-8')
            (artifacts / 'speaker_selection.json').write_text(json.dumps({'target_speaker_id': 'speaker_0'}), encoding='utf-8')
            (artifacts / 'speaker_regions.jsonl').write_text('', encoding='utf-8')
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': []}), encoding='utf-8')
            (artifacts / 'dataset_qc.json').write_text('{}', encoding='utf-8')
            (artifacts / 'export_manifest.json').write_text('[]', encoding='utf-8')
            (artifacts / 'export_audit.json').write_text('[]', encoding='utf-8')
            (artifacts / 'export_summary.json').write_text('{}', encoding='utf-8')
            (artifacts / 'native_export_clips').mkdir()
            with patch('speechcraft_dataset.generate_qc_scores.run_transcript_qc_stage', return_value={'clip_count': 0, 'scored_count': 0}), patch('speechcraft_dataset.generate_qc_scores.run_speaker_purity_stage', return_value={'clip_count': 0, 'scored_count': 0}):
                exit_code = generate_qc_scores_main(['--run-root', str(run_root), '--config', str(config_path), '--force'])
            self.assertEqual(exit_code, 0)
            self.assertFalse((artifacts / 'dataset_qc.json').exists())
            self.assertFalse((artifacts / 'export_manifest.json').exists())
            self.assertFalse((artifacts / 'export_audit.json').exists())
            self.assertFalse((artifacts / 'export_summary.json').exists())
            self.assertFalse((artifacts / 'native_export_clips').exists())

    def test_single_speaker_run_writes_source_artifacts_status_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source)
            exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'source_audio'])
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            summary = read_json(run_root / 'artifacts' / 'source_audio_summary.json')
            manifest = read_json(run_root / 'artifacts' / 'source_audio_manifest.json')
            config = read_json(run_root / 'config.json')
            self.assertTrue(status['ok'])
            self.assertEqual(status['stage'], 'source_audio')
            self.assertEqual(summary['source_count'], 1)
            self.assertEqual(manifest['sources'][0]['sample_rate'], 16000)
            self.assertEqual(config['mode'], 'single_speaker')
            self.assertTrue((run_root / 'runtime_versions.json').exists())
            self.assertIn('dataset worker started', (run_root / 'logs' / 'dataset_worker.log').read_text())
            self.assertTrue(config['config_hash'].startswith('sha256:'))

    def test_run_accepts_multiple_source_wavs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source_a = temp_dir / 'a.wav'
            source_b = temp_dir / 'b.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source_a, sample_rate=16000, duration_sec=0.1)
            write_silent_wav(source_b, sample_rate=48000, duration_sec=0.2)
            exit_code = main(['--run-root', str(run_root), '--source-wav', str(source_a), '--source-wav', str(source_b), '--single-speaker', '--stop-after', 'source_audio'])
            self.assertEqual(exit_code, 0)
            summary = read_json(run_root / 'artifacts' / 'source_audio_summary.json')
            self.assertEqual(summary['source_count'], 2)
            self.assertEqual(summary['sample_rates'], [16000, 48000])
            self.assertAlmostEqual(summary['total_duration_sec'], 0.3, places=3)

    def test_missing_source_writes_failure_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            exit_code = main(['--run-root', str(run_root), '--source-wav', str(temp_dir / 'missing.wav')])
            self.assertEqual(exit_code, 1)
            status = read_json(run_root / 'status.json')
            self.assertFalse(status['ok'])
            self.assertEqual(status['reason_codes'], ['dataset_worker_failed'])
            self.assertIn('Source WAV not found', status['error'])

    def test_audio_variants_stage_writes_mono_analysis_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=48000, duration_sec=0.1)
            exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'audio_variants'])
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            manifest = read_json(run_root / 'artifacts' / 'audio_variants_manifest.json')
            summary = read_json(run_root / 'artifacts' / 'audio_variants_summary.json')
            analysis_path = run_root / manifest['variants'][0]['path']
            self.assertEqual(status['stage'], 'audio_variants')
            self.assertEqual(summary['analysis_sample_rate'], 16000)
            self.assertTrue(summary['all_mono'])
            self.assertTrue(analysis_path.exists())
            variant = manifest['variants'][0]
            self.assertEqual(variant['source_sample_rate'], 48000)
            self.assertEqual(variant['analysis_sample_rate'], 16000)
            self.assertEqual(variant['source_start_sample'], 0)
            self.assertEqual(variant['analysis_start_sample'], 0)
            self.assertEqual(variant['recipe']['channel_mode'], 'mono_average')
            self.assertIn('source_audio', variant['input_artifact_hashes'])
            with wave.open(str(analysis_path), 'rb') as handle:
                self.assertEqual(handle.getframerate(), 16000)
                self.assertEqual(handle.getnchannels(), 1)

    def test_single_speaker_diarization_uses_internal_silero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=0.2)
            fake_summary = {
                'stage': 'diarization',
                'backend': 'single_speaker_vad_passthrough',
                'speaker_count': 1,
                'reason_codes': [],
            }
            with patch('speechcraft_dataset.run.run_diarization', return_value=fake_summary) as diarization:
                exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'diarization'])
            self.assertEqual(exit_code, 0)
            self.assertEqual(read_json(run_root / 'status.json')['stage'], 'diarization')
            self.assertEqual(read_json(run_root / 'status.json')['summary'], fake_summary)
            self.assertTrue((run_root / 'artifacts' / 'audio_variants_manifest.json').exists())
            diarization.assert_called_once()

    def test_missing_single_speaker_vad_dependency_writes_stage_specific_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=0.2)
            with patch('speechcraft_dataset.run.run_diarization', side_effect=RuntimeError('Silero VAD dependencies are unavailable: ModuleNotFoundError')):
                exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'diarization'])
            self.assertEqual(exit_code, 1)
            status = read_json(run_root / 'status.json')
            self.assertEqual(status['stage'], 'diarization')
            self.assertEqual(status['reason_codes'], ['missing_silero_vad_dependency'])

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_buffers_stage_writes_padded_processing_buffers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=1.0)

            def fake_vad(fake_run_root: Path, _config: dict) -> dict:
                (fake_run_root / 'artifacts' / 'vad_segments.jsonl').write_text(json.dumps({'id': 'source_audio_0000_vad_000000', 'source_audio_id': 'source_audio_0000', 'analysis_start_sample': 1600, 'analysis_end_sample': 12800, 'analysis_start_sec': 0.1, 'analysis_end_sec': 0.8, 'start_sample': 1600, 'end_sample': 12800}) + '\n', encoding='utf-8')
                (fake_run_root / 'artifacts' / 'vad_summary.json').write_text(json.dumps({'segment_count': 1}), encoding='utf-8')
                return {'segment_count': 1}
            with patch('speechcraft_dataset.diarization.run_silero_vad', side_effect=fake_vad):
                exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'buffers'])
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            buffers = read_json(run_root / 'artifacts' / 'processing_buffers.json')
            summary = read_json(run_root / 'artifacts' / 'processing_buffer_summary.json')
            selection = read_json(run_root / 'artifacts' / 'speaker_selection.json')
            buffer = buffers[0]
            self.assertEqual(status['stage'], 'buffers')
            self.assertEqual(summary['buffer_count'], 1)
            self.assertEqual(selection['target_speaker_id'], 'speaker_0')
            self.assertTrue(selection['selected'])
            self.assertEqual(buffer['trusted_start_sample'], 1600)
            self.assertEqual(buffer['trusted_end_sample'], 12800)
            self.assertEqual(buffer['source_start_sample'], 1600)
            self.assertEqual(buffer['source_end_sample'], 12800)
            self.assertEqual(buffer['trusted_local_start_sample'], 0)
            self.assertTrue((run_root / buffer['audio_path']).exists())
            self.assertEqual(buffer['split_strategy'], 'whole_region')
            self.assertIn('input_artifact_hashes', summary)
            self.assertIn('output_hashes', summary)

    def test_multi_speaker_run_stops_after_diarization_until_speaker_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=1.0)
            fake_diarization_summary = {'stage': 'diarization', 'speaker_count': 2, 'speaker_ids': ['speaker_0', 'speaker_1'], 'reason_codes': ['speaker_selection_required']}
            with patch('speechcraft_dataset.run.run_diarization', return_value=fake_diarization_summary), patch('speechcraft_dataset.run.run_processing_buffers') as buffers:
                exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--stop-after', 'candidate_review_clips'])
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            self.assertEqual(status['stage'], 'diarization')
            self.assertEqual(status['reason_codes'], ['speaker_selection_required'])
            buffers.assert_not_called()

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_buffers_stage_skips_sources_when_vad_detects_no_speech(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=1.0)

            def fake_vad(fake_run_root: Path, _config: dict) -> dict:
                (fake_run_root / 'artifacts' / 'vad_segments.jsonl').write_text('', encoding='utf-8')
                (fake_run_root / 'artifacts' / 'vad_summary.json').write_text(json.dumps({'segment_count': 0}), encoding='utf-8')
                return {'segment_count': 0}
            with patch('speechcraft_dataset.diarization.run_silero_vad', side_effect=fake_vad):
                exit_code = main(['--run-root', str(run_root), '--source-wav', str(source), '--single-speaker', '--stop-after', 'buffers'])
            self.assertEqual(exit_code, 0)
            buffers = read_json(run_root / 'artifacts' / 'processing_buffers.json')
            summary = read_json(run_root / 'artifacts' / 'processing_buffer_summary.json')
            self.assertEqual(buffers, [])
            self.assertEqual(summary['buffer_count'], 0)
            self.assertEqual(summary['skipped_sources'][0]['reason_codes'], ['no_speech_detected'])

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_buffers_stage_rejects_wrong_analysis_sample_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            analysis = run_root / 'audio' / 'analysis' / 'bad.wav'
            analysis.parent.mkdir(parents=True)
            write_silent_wav(analysis, sample_rate=8000, duration_sec=1.0)
            artifacts = run_root / 'artifacts'
            artifacts.mkdir()
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': [{'source_audio_id': 'source_audio_0000', 'path': 'audio/analysis/bad.wav'}]}), encoding='utf-8')
            (artifacts / 'vad_segments.jsonl').write_text(json.dumps({'source_audio_id': 'source_audio_0000', 'analysis_start_sample': 0, 'analysis_end_sample': 8000, 'analysis_start_sec': 0.0, 'analysis_end_sec': 1.0}) + '\n', encoding='utf-8')
            (artifacts / 'speaker_regions.jsonl').write_text(json.dumps({'id': 'speaker_0-a', 'source_audio_id': 'source_audio_0000', 'speaker_id': 'speaker_0', 'start_sample': 0, 'end_sample': 8000}) + '\n', encoding='utf-8')
            (artifacts / 'speaker_selection.json').write_text(json.dumps({'mode': 'single_speaker', 'selected': True, 'target_speaker_id': 'speaker_0', 'source': 'auto', 'available_speaker_ids': ['speaker_0']}), encoding='utf-8')
            with self.assertRaises(ValueError):
                run_processing_buffers(run_root, {'analysis_sample_rate': 16000})

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_buffers_stage_uses_selected_speaker_regions_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            analysis = run_root / 'audio' / 'analysis' / 'source_audio_0000.mono16000.wav'
            analysis.parent.mkdir(parents=True)
            write_silent_wav(analysis, sample_rate=16000, duration_sec=2.0)
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': [{'source_audio_id': 'source_audio_0000', 'path': 'audio/analysis/source_audio_0000.mono16000.wav', 'analysis_sample_rate': 16000}]}), encoding='utf-8')
            (artifacts / 'vad_segments.jsonl').write_text('\n'.join([json.dumps({'source_audio_id': 'source_audio_0000', 'analysis_start_sample': 1000, 'analysis_end_sample': 6000, 'analysis_start_sec': 0.0625, 'analysis_end_sec': 0.375}), json.dumps({'source_audio_id': 'source_audio_0000', 'analysis_start_sample': 9000, 'analysis_end_sample': 14000, 'analysis_start_sec': 0.5625, 'analysis_end_sec': 0.875})]) + '\n', encoding='utf-8')
            (artifacts / 'speaker_regions.jsonl').write_text('\n'.join([json.dumps({'id': 'speaker_0-a', 'source_audio_id': 'source_audio_0000', 'speaker_id': 'speaker_0', 'start_sample': 1000, 'end_sample': 6000}), json.dumps({'id': 'speaker_1-a', 'source_audio_id': 'source_audio_0000', 'speaker_id': 'speaker_1', 'start_sample': 9000, 'end_sample': 14000})]) + '\n', encoding='utf-8')
            (artifacts / 'speaker_selection.json').write_text(json.dumps({'mode': 'diarization', 'selected': True, 'target_speaker_id': 'speaker_1', 'source': 'user', 'available_speaker_ids': ['speaker_0', 'speaker_1']}), encoding='utf-8')
            summary = run_processing_buffers(run_root, {'analysis_sample_rate': 16000, 'mode': 'diarization'})
            buffers = read_json(artifacts / 'processing_buffers.json')
            self.assertEqual(summary['buffer_count'], 1)
            self.assertEqual(buffers[0]['target_speaker_id'], 'speaker_1')
            self.assertEqual(buffers[0]['trusted_start_sample'], 9000)
            self.assertEqual(buffers[0]['trusted_end_sample'], 14000)

    def test_asr_model_check_reports_missing_local_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            missing = Path(temp_dir_raw) / 'missing-model'
            result = check_asr_model(model='tiny.en', model_path=str(missing), local_only=True)
            self.assertFalse(result['ok'])
            self.assertEqual(result['source'], 'local_path')
            self.assertIn('does not exist', result['error'])

    def test_asr_model_check_reports_incomplete_snapshot_before_load(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            snapshot = Path(temp_dir_raw) / 'broken-medium'
            snapshot.mkdir(parents=True, exist_ok=True)
            (snapshot / 'config.json').write_text('{}', encoding='utf-8')
            result = check_asr_model(model='medium.en', model_path=str(snapshot), local_only=True, load_model=True)
            self.assertFalse(result['ok'])
            self.assertEqual(result['source'], 'local_path')
            self.assertIn('snapshot_check', result)
            self.assertEqual(result['snapshot_check']['missing_files'], ['model.bin'])
            self.assertEqual(result['error'], 'ASR model snapshot is incomplete: missing model.bin')
            self.assertFalse(result['load_checked'])

    def test_native_export_maps_analysis_samples_to_original_rate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            artifacts = run_root / 'artifacts'
            artifacts.mkdir(parents=True)
            source = temp_dir / 'source_48k.wav'
            write_silent_wav(source, sample_rate=48000, duration_sec=5.0)
            (artifacts / 'source_audio_manifest.json').write_text(json.dumps({'sources': [{'source_audio_id': 'source_audio_0000', 'source_recording_id': 'source', 'path': str(source), 'sample_rate': 48000, 'num_channels': 1, 'sample_width_bytes': 2, 'num_samples': 240000, 'duration_sec': 5.0}]}), encoding='utf-8')
            (artifacts / 'audio_variants_manifest.json').write_text(json.dumps({'variants': [{'source_audio_id': 'source_audio_0000', 'kind': 'analysis_audio', 'source_sample_rate': 48000, 'analysis_sample_rate': 16000, 'source_num_samples': 240000, 'analysis_num_samples': 80000}]}), encoding='utf-8')
            (artifacts / 'candidate_review_manifest.json').write_text(json.dumps([{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 48000, 'audio_path': 'artifacts/candidate_review_clips/candidate_review_clip_000000.wav', 'training_text': 'native rate please', 'alignment_text': 'native rate please', 'status': 'candidate_review', 'needs_review': False, 'review_reason_codes': [], 'start_cutpoint_ref': 'cut-0', 'end_cutpoint_ref': 'cut-1', 'word_ids': ['word-0']}]), encoding='utf-8')
            summary = export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})
            manifest = read_json(artifacts / 'export_manifest.json')
            exported = manifest[0]
            self.assertEqual(summary['exported_clip_count'], 1)
            self.assertEqual(summary['sample_rates'], [48000])
            self.assertEqual(exported['native_start_sample'], 48000)
            self.assertEqual(exported['native_end_sample'], 144000)
            self.assertEqual(exported['duration_samples'], 96000)
            self.assertEqual(exported['duration_sec'], 2.0)
            with wave.open(str(run_root / exported['audio_path']), 'rb') as handle:
                self.assertEqual(handle.getframerate(), 48000)
                self.assertEqual(handle.getnframes(), 96000)

    def test_export_native_falls_back_to_manifest_status_without_dataset_qc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            artifacts = write_minimal_native_export_fixture(run_root, temp_dir, [{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 32000, 'status': 'candidate_review', 'training_text': 'fallback export', 'alignment_text': 'fallback export'}, {'id': 'candidate_review_clip_000001', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 32000, 'source_end_sample': 48000, 'status': 'rejected', 'training_text': 'do not export', 'alignment_text': 'do not export'}])
            summary = export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})
            manifest = read_json(artifacts / 'export_manifest.json')
            audit = read_json(artifacts / 'export_audit.json')
            self.assertEqual(summary['qc_source'], 'artifacts/candidate_review_manifest.json')
            self.assertEqual(summary['exported_clip_count'], 1)
            self.assertEqual(manifest[0]['id'], 'candidate_review_clip_000000')
            self.assertEqual(audit[0]['candidate_id'], 'candidate_review_clip_000001')
            self.assertEqual(audit[0]['reason_codes'], ['candidate_status_not_exportable'])

    def test_export_native_prefers_dataset_qc_statuses_over_manifest_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            artifacts = write_minimal_native_export_fixture(run_root, temp_dir, [{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 32000, 'status': 'candidate_review', 'training_text': 'manifest says keep', 'alignment_text': 'manifest says keep'}, {'id': 'candidate_review_clip_000001', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 32000, 'source_end_sample': 48000, 'status': 'rejected', 'training_text': 'manifest says reject', 'alignment_text': 'manifest says reject'}], dataset_qc={'schema_version': 1, 'stage': 'dataset_qc', 'thresholds': {'transcript_match_min': 85, 'speaker_check_min': 70}, 'score_methods': {'transcript_match': 'whisper_b1_lj_v1', 'speaker_check': 'min_valid_window_similarity'}, 'manual_overrides': [], 'clips': [{'clip_id': 'candidate_review_clip_000000', 'status': 'rejected', 'manual_override': None}, {'clip_id': 'candidate_review_clip_000001', 'status': 'accepted', 'manual_override': None}]})
            summary = export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})
            manifest = read_json(artifacts / 'export_manifest.json')
            audit = read_json(artifacts / 'export_audit.json')
            self.assertEqual(summary['qc_source'], 'artifacts/dataset_qc.json')
            self.assertEqual(summary['qc_thresholds']['transcript_match_min'], 85)
            self.assertEqual(summary['qc_score_methods']['speaker_check'], 'min_valid_window_similarity')
            self.assertEqual(summary['manual_override_counts'], {'force_keep': 0, 'force_reject': 0})
            self.assertIsNotNone(summary['input_artifact_hashes']['dataset_qc_json'])
            self.assertEqual(summary['exported_clip_count'], 1)
            self.assertEqual(manifest[0]['id'], 'candidate_review_clip_000001')
            self.assertEqual(manifest[0]['qc_source'], 'artifacts/dataset_qc.json')
            self.assertEqual(manifest[0]['qc_status'], 'accepted')
            self.assertEqual(audit[0]['candidate_id'], 'candidate_review_clip_000000')
            self.assertEqual(audit[0]['reason_codes'], ['dataset_qc_status_not_accepted'])

    def test_export_native_uses_finalized_overrides_only_via_dataset_qc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            artifacts = write_minimal_native_export_fixture(run_root, temp_dir, [{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 32000, 'status': 'candidate_review', 'training_text': 'force kept clip', 'alignment_text': 'force kept clip'}, {'id': 'candidate_review_clip_000001', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 32000, 'source_end_sample': 48000, 'status': 'candidate_review', 'training_text': 'force rejected clip', 'alignment_text': 'force rejected clip'}], dataset_qc={'schema_version': 1, 'stage': 'dataset_qc', 'thresholds': {'transcript_match_min': 85, 'speaker_check_min': 70}, 'score_methods': {'transcript_match': 'whisper_b1_lj_v1', 'speaker_check': 'min_valid_window_similarity'}, 'manual_overrides': [{'clip_id': 'candidate_review_clip_000000', 'override': 'force_keep'}, {'clip_id': 'candidate_review_clip_000001', 'override': 'force_reject'}], 'clips': [{'clip_id': 'candidate_review_clip_000000', 'status': 'accepted', 'manual_override': 'force_keep'}, {'clip_id': 'candidate_review_clip_000001', 'status': 'rejected', 'manual_override': 'force_reject'}]})
            summary = export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})
            manifest = read_json(artifacts / 'export_manifest.json')
            audit = read_json(artifacts / 'export_audit.json')
            self.assertEqual(summary['manual_override_counts']['force_keep'], 1)
            self.assertEqual(summary['manual_override_counts']['force_reject'], 1)
            self.assertEqual(manifest[0]['id'], 'candidate_review_clip_000000')
            self.assertEqual(manifest[0]['manual_override'], 'force_keep')
            self.assertEqual(audit[0]['candidate_id'], 'candidate_review_clip_000001')
            self.assertEqual(audit[0]['manual_override'], 'force_reject')

    def test_export_native_rejects_duplicate_dataset_qc_clip_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            write_minimal_native_export_fixture(run_root, temp_dir, [{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 32000, 'status': 'candidate_review'}], dataset_qc={'schema_version': 1, 'stage': 'dataset_qc', 'thresholds': {'transcript_match_min': 85, 'speaker_check_min': 70}, 'score_methods': {'transcript_match': 'whisper_b1_lj_v1', 'speaker_check': 'min_valid_window_similarity'}, 'manual_overrides': [], 'clips': [{'clip_id': 'candidate_review_clip_000000', 'status': 'accepted', 'manual_override': None}, {'clip_id': 'candidate_review_clip_000000', 'status': 'rejected', 'manual_override': None}]})
            with self.assertRaisesRegex(ValueError, 'duplicate clip_id in dataset_qc.json'):
                export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})

    def test_export_native_rejects_invalid_dataset_qc_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            run_root = temp_dir / 'run'
            write_minimal_native_export_fixture(run_root, temp_dir, [{'id': 'candidate_review_clip_000000', 'source_audio_id': 'source_audio_0000', 'source_start_sample': 16000, 'source_end_sample': 32000, 'status': 'candidate_review'}], dataset_qc={'schema_version': 1, 'stage': 'dataset_qc', 'thresholds': {'transcript_match_min': 85, 'speaker_check_min': 70}, 'score_methods': {'transcript_match': 'whisper_b1_lj_v1', 'speaker_check': 'min_valid_window_similarity'}, 'manual_overrides': [], 'clips': [{'clip_id': 'candidate_review_clip_000000', 'status': 'maybe', 'manual_override': None}]})
            with self.assertRaisesRegex(ValueError, 'invalid status'):
                export_native_candidate_clips(run_root, {'config_hash': 'sha256:test'})

    def test_live_worker_stages_exclude_mfa_and_asr_slicing(self) -> None:
        self.assertEqual(
            WORKER_STAGE_ORDER,
            [
                'source_audio',
                'audio_variants',
                'diarization',
                'buffers',
                'candidate_review_clips',
                'transcript_qc',
                'speaker_purity',
                'native_export',
            ],
        )
        for obsolete in ('asr', 'asr_queue', 'normalization', 'mfa', 'alignment_qc', 'safe_cutpoints', 'vad'):
            self.assertNotIn(obsolete, WORKER_STAGE_ORDER)
        for module_name in (
            'speechcraft_dataset.mfa',
            'speechcraft_dataset.asr',
            'speechcraft_dataset.safecut',
            'speechcraft_dataset.alignment_qc',
        ):
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(module_name)

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_candidate_review_assembly_writes_vr_native_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            artifacts = run_root / 'artifacts'
            analysis = run_root / 'audio' / 'analysis' / 'source_audio_0000.mono16000.wav'
            analysis.parent.mkdir(parents=True)
            write_silent_wav(analysis, sample_rate=16000, duration_sec=10.0)
            artifacts.mkdir()
            (artifacts / 'processing_buffers.json').write_text(
                json.dumps(
                    [
                        {
                            'buffer_id': 'buffer_000000',
                            'source_audio_id': 'source_audio_0000',
                            'analysis_audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'sample_rate': 16000,
                            'trusted_start_sample': 0,
                            'trusted_end_sample': 160000,
                            'trusted_start_sec': 0.0,
                            'trusted_end_sec': 10.0,
                            'source_start_sample': 0,
                            'source_end_sample': 160000,
                            'source_start_sec': 0.0,
                            'source_end_sec': 10.0,
                        }
                    ]
                ),
                encoding='utf-8',
            )
            fake_clip = VrPackedClip(
                detector_name='vad_percentile_rms',
                packer_name='optimal_weighted_interval',
                source_id='source_audio_0000',
                recording_id='source_audio_0000',
                buffer_id='buffer_000000',
                clip_id='clip-0',
                start_sec=1.0,
                end_sec=9.0,
                duration_sec=8.0,
                start_cutpoint_id='cut-a',
                end_cutpoint_id='cut-b',
                selected_weight=1.0,
            )
            fake_result = VrSlicerResult(
                geometry_fingerprint=TRUSTED_GEOMETRY_FINGERPRINT,
                cutpoints=(),
                clips=(fake_clip,),
                selected_cutpoints=(),
            )
            with patch('speechcraft_dataset.assembly.slice_wav', return_value=fake_result):
                summary = assemble_candidate_review_clips(run_root, {'analysis_sample_rate': 16000, 'config_hash': 'sha256:test'})
            manifest = read_json(artifacts / 'candidate_review_manifest.json')
            clip = manifest[0]
            clip_path = run_root / clip['audio_path']
            self.assertEqual(summary['slicer'], 'VR')
            self.assertEqual(summary['slicer_geometry'], 'O0_4')
            self.assertEqual(clip['id'], 'candidate_review_clip_000000')
            self.assertEqual(clip['training_text'], '')
            self.assertNotIn('word_ids', clip)
            self.assertNotIn('alignment_text', clip)
            self.assertEqual(clip['slicer'], 'VR')
            self.assertEqual(clip['audio_sha256'], sha256_file(clip_path))
            self.assertEqual(clip['audio_hash'], clip['audio_sha256'])
            self.assertEqual(clip['start_cutpoint_ref'], 'cut-a')
            self.assertEqual(clip['end_cutpoint_ref'], 'cut-b')
            self.assertTrue((artifacts / 'vr_cutpoints.jsonl').exists())

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_candidate_assembly_reads_each_source_wav_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            artifacts = run_root / 'artifacts'
            analysis = run_root / 'audio' / 'analysis' / 'source_audio_0000.mono16000.wav'
            analysis.parent.mkdir(parents=True)
            write_silent_wav(analysis, sample_rate=16000, duration_sec=20.0)
            artifacts.mkdir()
            (artifacts / 'processing_buffers.json').write_text(
                json.dumps(
                    [
                        {
                            'buffer_id': 'buffer_000000',
                            'source_audio_id': 'source_audio_0000',
                            'analysis_audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'sample_rate': 16000,
                            'trusted_start_sample': 0,
                            'trusted_end_sample': 320000,
                            'trusted_start_sec': 0.0,
                            'trusted_end_sec': 20.0,
                            'source_start_sample': 0,
                            'source_end_sample': 320000,
                        }
                    ]
                ),
                encoding='utf-8',
            )
            clips = (
                VrPackedClip(
                    detector_name='vad_percentile_rms',
                    packer_name='optimal_weighted_interval',
                    source_id='source_audio_0000',
                    recording_id='source_audio_0000',
                    buffer_id='buffer_000000',
                    clip_id='clip-0',
                    start_sec=1.0,
                    end_sec=9.0,
                    duration_sec=8.0,
                    start_cutpoint_id='cut-a',
                    end_cutpoint_id='cut-b',
                    selected_weight=1.0,
                ),
                VrPackedClip(
                    detector_name='vad_percentile_rms',
                    packer_name='optimal_weighted_interval',
                    source_id='source_audio_0000',
                    recording_id='source_audio_0000',
                    buffer_id='buffer_000000',
                    clip_id='clip-1',
                    start_sec=10.0,
                    end_sec=18.0,
                    duration_sec=8.0,
                    start_cutpoint_id='cut-c',
                    end_cutpoint_id='cut-d',
                    selected_weight=1.0,
                ),
            )
            fake_result = VrSlicerResult(
                geometry_fingerprint=TRUSTED_GEOMETRY_FINGERPRINT,
                cutpoints=(),
                clips=clips,
                selected_cutpoints=(),
            )
            with (
                patch('speechcraft_dataset.assembly.slice_wav', return_value=fake_result),
                patch('speechcraft_dataset.assembly.read_analysis_audio', wraps=read_analysis_audio) as read_audio,
            ):
                summary = assemble_candidate_review_clips(run_root, {'analysis_sample_rate': 16000, 'config_hash': 'sha256:test'})
            self.assertEqual(summary['candidate_review_clips'], 2)
            self.assertEqual(read_audio.call_count, 1)

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_candidate_assembly_rejects_empty_buffers_even_when_source_emits_clips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            run_root = Path(temp_dir_raw)
            artifacts = run_root / 'artifacts'
            analysis = run_root / 'audio' / 'analysis' / 'source_audio_0000.mono16000.wav'
            analysis.parent.mkdir(parents=True)
            write_silent_wav(analysis, sample_rate=16000, duration_sec=20.0)
            artifacts.mkdir()
            (artifacts / 'processing_buffers.json').write_text(
                json.dumps(
                    [
                        {
                            'buffer_id': 'buffer_000000',
                            'source_audio_id': 'source_audio_0000',
                            'analysis_audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'sample_rate': 16000,
                            'trusted_start_sample': 0,
                            'trusted_end_sample': 160000,
                            'trusted_start_sec': 0.0,
                            'trusted_end_sec': 10.0,
                            'source_start_sample': 0,
                            'source_end_sample': 160000,
                        },
                        {
                            'buffer_id': 'buffer_000001',
                            'source_audio_id': 'source_audio_0000',
                            'analysis_audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'audio_path': 'audio/analysis/source_audio_0000.mono16000.wav',
                            'sample_rate': 16000,
                            'trusted_start_sample': 160000,
                            'trusted_end_sample': 320000,
                            'trusted_start_sec': 10.0,
                            'trusted_end_sec': 20.0,
                            'source_start_sample': 160000,
                            'source_end_sample': 320000,
                        },
                    ]
                ),
                encoding='utf-8',
            )
            fake_clip = VrPackedClip(
                detector_name='vad_percentile_rms',
                packer_name='optimal_weighted_interval',
                source_id='source_audio_0000',
                recording_id='source_audio_0000',
                buffer_id='buffer_000000',
                clip_id='clip-0',
                start_sec=1.0,
                end_sec=9.0,
                duration_sec=8.0,
                start_cutpoint_id='cut-a',
                end_cutpoint_id='cut-b',
                selected_weight=1.0,
            )
            fake_result = VrSlicerResult(
                geometry_fingerprint=TRUSTED_GEOMETRY_FINGERPRINT,
                cutpoints=(),
                clips=(fake_clip,),
                selected_cutpoints=(),
            )
            with patch('speechcraft_dataset.assembly.slice_wav', return_value=fake_result):
                summary = assemble_candidate_review_clips(run_root, {'analysis_sample_rate': 16000, 'config_hash': 'sha256:test'})
            rejected = read_json(artifacts / 'candidate_review_rejected.json')
            self.assertEqual(summary['candidate_review_clips'], 1)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]['buffer_id'], 'buffer_000001')
            self.assertEqual(rejected[0]['reason_codes'], ['vr_slicer_emitted_no_clips'])

    @unittest.skipUnless(HAS_WORKER_AUDIO_DEPS, 'requires worker audio deps')
    def test_pipeline_ingest_to_candidates_does_not_require_whisper_or_mfa(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            source = temp_dir / 'source.wav'
            run_root = temp_dir / 'run'
            write_silent_wav(source, sample_rate=16000, duration_sec=1.0)

            def fake_vad(fake_run_root: Path, _config: dict) -> dict:
                (fake_run_root / 'artifacts' / 'vad_segments.jsonl').write_text(
                    json.dumps(
                        {
                            'id': 'source_audio_0000_vad_000000',
                            'source_audio_id': 'source_audio_0000',
                            'analysis_start_sample': 0,
                            'analysis_end_sample': 16000,
                            'analysis_start_sec': 0.0,
                            'analysis_end_sec': 1.0,
                            'start_sample': 0,
                            'end_sample': 16000,
                        }
                    )
                    + '\n',
                    encoding='utf-8',
                )
                (fake_run_root / 'artifacts' / 'vad_summary.json').write_text(json.dumps({'segment_count': 1}), encoding='utf-8')
                return {'segment_count': 1}

            with patch('speechcraft_dataset.diarization.run_silero_vad', side_effect=fake_vad):
                exit_code = main(
                    [
                        '--run-root',
                        str(run_root),
                        '--source-wav',
                        str(source),
                        '--single-speaker',
                        '--stop-after',
                        'candidate_review_clips',
                    ]
                )
            self.assertEqual(exit_code, 0)
            status = read_json(run_root / 'status.json')
            self.assertEqual(status['stage'], 'candidate_review_clips')
            self.assertTrue((run_root / 'artifacts' / 'candidate_review_manifest.json').exists())
            self.assertFalse((run_root / 'artifacts' / 'transcripts.json').exists())
            self.assertFalse((run_root / 'artifacts' / 'aligned_words.jsonl').exists())
            self.assertFalse((run_root / 'artifacts' / 'safe_cutpoints.jsonl').exists())
            self.assertNotIn('speechcraft_dataset.mfa', sys.modules)
            self.assertNotIn('speechcraft_dataset.asr', sys.modules)
            self.assertNotIn('faster_whisper', sys.modules)
if __name__ == '__main__':
    unittest.main()
