import assert from 'node:assert/strict';
import test from 'node:test';
import { ByteTransferSampler, DownloadProgressTracker, safeDisplayFilename, sanitizeProgressTelemetry } from '../src/download-progress.js';

const base = { event: 'progress', path: 'weights/model.bin', total_bytes: 1_000, downloaded_bytes: 100,
  transfer_attempt_bytes: 0, attempt: 1, files_completed: 0, files_total: 1 };

test('generic runtime downloads report percentage, measured speed, and ETA from actual bytes', () => {
  let now = 0;
  const sampler = new ByteTransferSampler({ clock: () => now });
  const initial = sampler.update(1_000, 11_000);
  assert.equal(initial.bytes_per_second, null, 'a single sample does not invent a rate');
  assert.equal(initial.eta_seconds, null);
  now = 2_000;
  const measured = sampler.update(5_000, 11_000);
  assert.equal(measured.bytes_per_second, 2_000);
  assert.equal(measured.eta_seconds, 3);
});

test('generic runtime download telemetry rejects unknown or invalid totals without estimates', () => {
  const sampler = new ByteTransferSampler({ clock: () => 0 });
  assert.deepEqual(sampler.update(10, null), { bytes_per_second: null, eta_seconds: null });
  assert.deepEqual(sampler.update(20, 10), { bytes_per_second: null, eta_seconds: null });
});

test('direct transfer uses actual network deltas, excludes resumed bytes, and estimates ETA', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  const resumed = tracker.accept({ ...base, event: 'downloading' }, 'asr', 'Whisper Large-v3');
  assert.equal(resumed.transferred_bytes, 100, 'resumed bytes remain visible in download progress');
  assert.equal(resumed.bytes_per_second, null, 'cached prefix does not count as transfer speed');
  now = 1_000;
  const update = tracker.accept({ ...base, downloaded_bytes: 600, transfer_attempt_bytes: 500 }, 'asr', 'Whisper Large-v3');
  assert.equal(update.bytes_per_second, 500);
  assert.equal(update.eta_seconds, 1);
  assert.equal(update.current_filename, 'model.bin');
});

test('unknown total remains byte-indeterminate and never invents an ETA', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, downloaded_bytes: null, total_bytes: 10_000, transfer_attempt_bytes: null,
    transfer_indeterminate: true, files_completed: 2, files_total: 8 }, 'diarization', 'pyannote Community-1');
  now = 1_000;
  const result = tracker.accept({ ...base, event: 'source_progress', downloaded_bytes: null,
    transferred_bytes: null, transfer_attempt_bytes: null, transfer_indeterminate: true,
    total_bytes: 10_000, files_completed: 3, files_total: 8 }, 'diarization', 'pyannote Community-1');
  assert.equal(result.progress_mode, 'files');
  assert.equal(result.transferred_bytes, undefined);
  assert.equal(result.total_bytes, undefined);
  assert.equal(result.completed_files, 3);
  assert.equal(result.total_files, 8);
  assert.equal(result.bytes_per_second, null);
  assert.equal(result.eta_seconds, null);
  assert.equal(result.transfer_indeterminate, true);
});

test('known downloaded bytes with unknown total retain size and speed but no percentage or ETA basis', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, event: 'downloading', downloaded_bytes: 0, total_bytes: null }, 'speech_detection', 'Silero VAD');
  now = 1_000;
  const result = tracker.accept({ ...base, downloaded_bytes: 500, total_bytes: null,
    transfer_attempt_bytes: 500 }, 'speech_detection', 'Silero VAD');
  assert.equal(result.progress_mode, 'files');
  assert.equal(result.transferred_bytes, undefined);
  assert.equal(result.bytes_per_second, null);
  assert.equal(result.eta_seconds, null);
});

test('stall clears measured speed and resume starts a fresh speed window', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, event: 'downloading' }, 'asr', 'Whisper Large-v3');
  now = 1_000;
  tracker.accept({ ...base, downloaded_bytes: 500, transfer_attempt_bytes: 400 }, 'asr', 'Whisper Large-v3');
  now = 4_100;
  const stalled = tracker.stalled();
  assert.equal(stalled.stalled, true);
  assert.equal(stalled.bytes_per_second, null);
  assert.equal(stalled.eta_seconds, null);
  now = 5_000;
  const resumed = tracker.accept({ ...base, downloaded_bytes: 700, transfer_attempt_bytes: 600 }, 'asr', 'Whisper Large-v3');
  assert.equal(resumed.stalled, false);
  assert.equal(resumed.bytes_per_second, null, 'speed waits for two fresh post-stall samples');
});

test('retry and transfer changes reset speed samples', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, event: 'downloading' }, 'asr', 'Whisper Large-v3');
  now = 1_000;
  assert.ok(tracker.accept({ ...base, downloaded_bytes: 500, transfer_attempt_bytes: 400 }, 'asr', 'Whisper Large-v3').bytes_per_second);
  const retry = tracker.accept({ ...base, event: 'downloading', attempt: 2, downloaded_bytes: 400,
    transfer_attempt_bytes: 0 }, 'asr', 'Whisper Large-v3');
  assert.equal(retry.bytes_per_second, null);
  now = 2_000;
  assert.equal(tracker.accept({ ...base, event: 'progress', attempt: 2, downloaded_bytes: 800,
    transfer_attempt_bytes: 400 }, 'asr', 'Whisper Large-v3').bytes_per_second, 400);
  const nextFile = tracker.accept({ ...base, path: 'weights/vocabulary.json', attempt: 1,
    downloaded_bytes: 850, transfer_attempt_bytes: 0 }, 'asr', 'Whisper Large-v3');
  assert.equal(nextFile.bytes_per_second, null);
});

test('completion and phase changes do not retain a download ETA or speed', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, event: 'downloading', downloaded_bytes: 0 }, 'asr', 'Whisper Large-v3');
  now = 1_000;
  tracker.accept({ ...base, downloaded_bytes: 500, transfer_attempt_bytes: 500 }, 'asr', 'Whisper Large-v3');
  now = 2_000;
  const complete = tracker.accept({ ...base, downloaded_bytes: 1_000, total_bytes: 1_000,
    transfer_attempt_bytes: 1_000 }, 'asr', 'Whisper Large-v3');
  assert.equal(complete.transferred_bytes, complete.total_bytes);
  const verifying = tracker.accept({ event: 'verifying', total_bytes: 1_000 }, 'asr', 'Whisper Large-v3');
  assert.equal(verifying.phase, 'verifying');
  assert.equal(verifying.bytes_per_second, undefined);
  assert.equal(verifying.eta_seconds, undefined);
  tracker.reset();
  assert.equal(tracker.stalled(), null, 'finished or interrupted child processes leave no active speed estimate');
});

test('setup phases survive progress sanitization while unknown phases are omitted', () => {
  for (const phase of ['queued', 'inspecting', 'installing', 'verifying', 'complete']) {
    assert.equal(sanitizeProgressTelemetry({ phase }).phase, phase);
  }
  assert.equal(sanitizeProgressTelemetry({ phase: 'future_setup_step' }).phase, undefined);
  assert.equal(sanitizeProgressTelemetry({ downloaded_bytes: 50 }).phase, undefined);
  assert.equal(sanitizeProgressTelemetry({ phase: 'downloading', artifact_name: 'uv 0.12.23' }).artifact_name, 'uv 0.12.23');
});

test('unknown-size progress events are throttled and valid completion emits immediately', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  const unknown = tracker.accept({ ...base, event: 'source_progress', path: 'snapshot/part.bin',
    downloaded_bytes: null, total_bytes: null, transfer_attempt_bytes: null,
    transfer_indeterminate: true, files_completed: 1, files_total: 8 }, 'diarization', 'pyannote Community-1');
  let emissions = Number(tracker.shouldEmit(unknown));
  for (let index = 0; index < 1_000; index += 1) {
    now += 1;
    const progress = tracker.accept({ ...base, event: 'source_progress', path: 'snapshot/part.bin',
      downloaded_bytes: null, total_bytes: null, transfer_attempt_bytes: null,
      transfer_indeterminate: true, files_completed: 1, files_total: 8 }, 'diarization', 'pyannote Community-1');
    if (tracker.shouldEmit(progress)) emissions += 1;
  }
  assert.equal(emissions, 2, 'a burst emits once initially and again only after the throttle interval');

  now += 1;
  const completed = tracker.accept({ ...base, event: 'source_progress', path: 'snapshot/part.bin',
    downloaded_bytes: 1_000, total_bytes: 1_000, transfer_attempt_bytes: 1_000 }, 'diarization', 'pyannote Community-1');
  assert.equal(tracker.shouldEmit(completed), true, 'genuine completion bypasses the throttle');
});

test('Hugging Face production progress shape reports file counts without byte percent', () => {
  const tracker = new DownloadProgressTracker({ clock: () => 0 });
  const event = tracker.accept({ event: 'source_progress', source: 'huggingface', operation: 'downloading',
    progress_mode: 'files', current_file: null, completed_files: 2, total_files: 6 }, 'asr',
  'Systran/faster-whisper-large-v3');
  assert.equal(event.artifact_name, 'Systran/faster-whisper-large-v3');
  assert.equal(event.artifact_type, 'model');
  assert.equal(event.progress_mode, 'files');
  assert.equal(event.completed_files, 2);
  assert.equal(event.total_files, 6);
  assert.equal(Object.hasOwn(event, 'percent'), false, 'file completion is never a byte percentage');
  const publicEvent = sanitizeProgressTelemetry({ ...event, job_id: 'job-123', attempt_id: 'attempt-2', event_revision: 17 });
  assert.equal(publicEvent.percent, undefined);
  assert.equal(publicEvent.progress_mode, 'files');
  assert.equal(publicEvent.artifact_name, 'Systran/faster-whisper-large-v3');
  assert.equal(publicEvent.job_id, 'job-123');
  assert.equal(publicEvent.event_revision, 17);
});

test('small completed files cannot make a multi-file model appear fully downloaded', () => {
  const tracker = new DownloadProgressTracker({ clock: () => 0 });
  const event = tracker.accept({ event: 'source_progress', source: 'huggingface', operation: 'downloading',
    progress_mode: 'files', completed_files: 5, total_files: 6 }, 'asr', 'Systran/faster-whisper-large-v3');
  assert.equal(event.progress_mode, 'files');
  assert.equal(event.completed_files, 5);
  assert.equal(event.percent, undefined);
});

test('explicit indeterminate mode is preserved even if incidental file counts exist', () => {
  const value = sanitizeProgressTelemetry({ operation: 'downloading', progress_mode: 'indeterminate',
    completed_files: 3, total_files: 4 });
  assert.equal(value.progress_mode, 'indeterminate');
  assert.equal(value.completed_files, 3);
  assert.equal(value.percent, undefined);
});

test('artifact changes reset speed and percent follows only the current artifact byte counters', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  tracker.accept({ ...base, event: 'downloading', downloaded_bytes: 0, total_bytes: 1_000,
    transfer_attempt_bytes: 0, artifact_name: 'model-A' }, 'asr');
  now = 1_000;
  const first = tracker.accept({ ...base, downloaded_bytes: 500, total_bytes: 1_000,
    transfer_attempt_bytes: 500, artifact_name: 'model-A' }, 'asr');
  assert.equal(first.percent, 50);
  assert.equal(first.bytes_per_second, 500);
  const second = tracker.accept({ ...base, downloaded_bytes: 0, total_bytes: 10_000,
    transfer_attempt_bytes: 0, artifact_name: 'model-B' }, 'asr');
  assert.equal(second.percent, 0);
  assert.equal(second.bytes_per_second, null);
  assert.equal(second.artifact_name, 'model-B');
});

test('install and verification events carry operation labels without download measurements', () => {
  const tracker = new DownloadProgressTracker({ clock: () => 0 });
  const verifying = tracker.accept({ event: 'verifying', operation: 'verifying' }, 'asr', 'model-A');
  assert.equal(verifying.operation, 'verifying');
  assert.equal(verifying.progress_mode, 'indeterminate');
  assert.equal(verifying.percent, undefined);
  const installing = sanitizeProgressTelemetry({ operation: 'installing', artifact_type: 'python_package',
    artifact_name: 'Installing dependencies', transferred_bytes: 500, total_bytes: 1_000 });
  assert.equal(installing.operation, 'installing');
  assert.equal(installing.progress_mode, 'indeterminate');
  assert.equal(installing.percent, undefined);
});

test('build and extraction operations remain indeterminate and sanitize local paths', () => {
  const building = sanitizeProgressTelemetry({ operation: 'building', artifact_name: 'FFmpeg n8.1.3',
    artifact_type: 'runtime', progress_mode: 'bytes', transferred_bytes: 42, total_bytes: 84,
    message: 'Building in /home/private-user/.cache/tmp' });
  assert.equal(building.operation, 'building');
  assert.equal(building.progress_mode, 'indeterminate');
  assert.equal(building.transferred_bytes, undefined);
  assert.match(building.message, /\[local path removed\]/);
});

test('observed model artifact names are validated before they can enter component messages', () => {
  const tracker = new DownloadProgressTracker({ clock: () => 0 });
  const unsafe = tracker.accept({ event: 'downloading', role: 'asr', artifact_name: '/home/private/model.bin',
    downloaded_bytes: 1, total_bytes: 2, transfer_attempt_bytes: 1, attempt: 1 }, 'asr');
  assert.equal(unsafe.artifact_name, null);
  const trusted = tracker.accept({ event: 'downloading', role: 'asr', artifact_name: 'Systran/faster-whisper-large-v3',
    downloaded_bytes: 1, total_bytes: 2, transfer_attempt_bytes: 1, attempt: 1 }, 'asr');
  assert.equal(trusted.artifact_name, 'Systran/faster-whisper-large-v3');
});

test('malformed, out-of-order, or secret-bearing progress cannot become public telemetry', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  assert.equal(tracker.accept({ ...base, downloaded_bytes: -1, transfer_attempt_bytes: -1 }, 'asr', 'Whisper'), null);
  const first = tracker.accept({ ...base, event: 'downloading', transfer_attempt_bytes: 100 }, 'asr', 'Whisper Large-v3');
  now = 1_000;
  const backwards = tracker.accept({ ...base, downloaded_bytes: 50, transfer_attempt_bytes: 10 }, 'asr', 'Whisper Large-v3');
  assert.equal(backwards, null, 'a decreasing network counter in one attempt is discarded as out of order');
  assert.equal(safeDisplayFilename('https://host.invalid/signed?token=secret'), null);
  assert.equal(safeDisplayFilename('hf_abcdefghijklmnop1234'), null);
  assert.equal(safeDisplayFilename('../../token.json'), 'token.json');
  assert.equal(sanitizeProgressTelemetry({ operation: 'downloading', artifact_name: '/tmp/cache/model.bin' }).artifact_name, undefined);
  assert.equal(sanitizeProgressTelemetry({ operation: 'downloading', artifact_name: 'a'.repeat(64) }).artifact_name, undefined);
  assert.equal(sanitizeProgressTelemetry({ operation: 'downloading', artifact_name: 'Systran/faster-whisper-large-v3' }).artifact_name,
    'Systran/faster-whisper-large-v3');
  const safe = sanitizeProgressTelemetry({ ...first, message: 'download https://cdn.invalid/?token=secret hf_abcdefghijklmnop',
    signed_url: 'https://cdn.invalid/?token=secret',
    authorization: 'hf_secret', headers: { authorization: 'secret' } });
  const encoded = JSON.stringify(safe);
  assert.doesNotMatch(encoded, /signed_url|authorization|headers|secret|https?:/i);
  assert.equal(sanitizeProgressTelemetry({ phase: 'downloading', downloaded_bytes: 10_000,
    total_bytes: 1_000, percent: 900 }).percent, undefined);
});
