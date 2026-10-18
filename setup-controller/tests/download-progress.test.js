import assert from 'node:assert/strict';
import test from 'node:test';
import { DownloadProgressTracker, safeDisplayFilename, sanitizeProgressTelemetry } from '../src/download-progress.js';

const base = { event: 'progress', path: 'weights/model.bin', total_bytes: 1_000, downloaded_bytes: 100,
  transfer_attempt_bytes: 0, attempt: 1, files_completed: 0, files_total: 1 };

test('direct transfer uses actual network deltas, excludes resumed bytes, and estimates ETA', () => {
  let now = 0;
  const tracker = new DownloadProgressTracker({ clock: () => now });
  const resumed = tracker.accept({ ...base, event: 'downloading' }, 'asr', 'Whisper Large-v3');
  assert.equal(resumed.downloaded_bytes, 100, 'resumed bytes remain visible in download progress');
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
  assert.equal(result.downloaded_bytes, null);
  assert.equal(result.total_bytes, 10_000);
  assert.equal(result.files_completed, 3);
  assert.equal(result.files_total, 8);
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
  assert.equal(result.downloaded_bytes, 500);
  assert.equal(result.total_bytes, null);
  assert.equal(result.bytes_per_second, 500);
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
  assert.equal(complete.downloaded_bytes, complete.total_bytes);
  const verifying = tracker.accept({ event: 'verifying', total_bytes: 1_000 }, 'asr', 'Whisper Large-v3');
  assert.equal(verifying.phase, 'verifying');
  assert.equal(verifying.bytes_per_second, null);
  assert.equal(verifying.eta_seconds, null);
  tracker.reset();
  assert.equal(tracker.stalled(), null, 'finished or interrupted child processes leave no active speed estimate');
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
  const safe = sanitizeProgressTelemetry({ ...first, message: 'download https://cdn.invalid/?token=secret hf_abcdefghijklmnop',
    signed_url: 'https://cdn.invalid/?token=secret',
    authorization: 'hf_secret', headers: { authorization: 'secret' } });
  const encoded = JSON.stringify(safe);
  assert.doesNotMatch(encoded, /signed_url|authorization|headers|secret|https?:/i);
  assert.equal(sanitizeProgressTelemetry({ phase: 'downloading', downloaded_bytes: 10_000,
    total_bytes: 1_000, percent: 900 }).percent, 100);
});
