import { performance } from 'node:perf_hooks';

const MAX_SAFE_BYTES = Number.MAX_SAFE_INTEGER;
const STALL_AFTER_MS = 3_000;
const SAMPLE_WINDOW_MS = 5_000;
const MIN_SAMPLE_MS = 250;
const MIN_EMIT_INTERVAL_MS = 750;
const MIN_EMIT_BYTES = 1024 * 1024;
const OPERATIONS = new Set(['checking', 'resolving', 'downloading', 'installing', 'extracting', 'building',
  'verifying', 'loading', 'starting', 'completed', 'failed', 'retrying']);
const PROGRESS_MODES = new Set(['bytes', 'files', 'indeterminate']);
const SETUP_PHASES = new Set([
  'queued', 'inspecting', 'downloading', 'verifying', 'verified', 'installing',
  'running_inference', 'interrupted', 'complete',
]);

function byteCount(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= MAX_SAFE_BYTES ? value : null;
}

function safeIdentifier(value) {
  return typeof value === 'string' && value.length <= 80 && /^[A-Za-z0-9-]+$/.test(value) ? value : null;
}

export function safeArtifactName(value) {
  if (typeof value !== 'string' || value.length > 160 || /[\u0000-\u001f\u007f\\]/.test(value)
      || /https?:|hf_[A-Za-z0-9_-]{16,}/i.test(value) || /^[a-f0-9]{32,}$/i.test(value)) return null;
  if (value.includes('/')) {
    return /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/.test(value) ? value : null;
  }
  return value.trim() === value && value.length > 0 ? value : null;
}

export function safeDisplayFilename(value) {
  if (typeof value !== 'string' || value.length > 1024 || /[\u0000-\u001f\u007f]/.test(value)
      || /^https?:/i.test(value) || /[?&](?:token|signature|x-amz-|expires=)/i.test(value)
      || /hf_[A-Za-z0-9_-]{16,}/i.test(value)) return null;
  const name = value.split(/[\\/]/).at(-1)?.trim();
  if (!name || name === '.' || name === '..' || name.includes(':') || /^https?:/i.test(name)) return null;
  const safe = name.replace(/[^\p{L}\p{N}._ -]/gu, '_').slice(0, 96);
  return safe || null;
}

function operationFor(value) {
  if (OPERATIONS.has(value.operation)) return value.operation;
  if (value.phase === 'downloading' || value.phase === 'source_acquiring') return 'downloading';
  if (value.phase === 'verifying' || value.phase === 'verified') return 'verifying';
  if (value.phase === 'installing' || value.phase === 'complete') return 'installing';
  if (value.phase === 'running_inference') return 'loading';
  if (value.phase === 'inspecting' || value.phase === 'queued') return 'checking';
  return null;
}

function progressModeFor(value, operation, transferred, total, completed, filesTotal) {
  if (operation !== 'downloading') return 'indeterminate';
  const requested = PROGRESS_MODES.has(value.progress_mode) ? value.progress_mode : null;
  const bytesValid = transferred !== null && total !== null && total > 0 && transferred <= total;
  if (requested === 'indeterminate') return 'indeterminate';
  if (requested === 'bytes') return bytesValid ? 'bytes' : 'indeterminate';
  if (requested === 'files') return completed !== null && filesTotal !== null && filesTotal > 0 ? 'files' : 'indeterminate';
  if (!requested && !value.transfer_indeterminate && bytesValid) return 'bytes';
  if (completed !== null && filesTotal !== null && filesTotal > 0) return 'files';
  return 'indeterminate';
}

/** Sanitize and normalize the public progress contract. File counts never become byte percentages. */
export function sanitizeProgressTelemetry(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const roles = new Set(['speech_detection', 'asr', 'diarization', 'speaker_purity']);
  const result = {};
  const operation = operationFor(value);
  if (operation) result.operation = operation;
  if (SETUP_PHASES.has(value.phase)) result.phase = value.phase;
  if (roles.has(value.role)) result.role = value.role;
  const artifactName = safeArtifactName(value.artifact_name);
  if (artifactName) result.artifact_name = artifactName;
  if (typeof value.model_name === 'string' && value.model_name.length <= 160
      && !/[\u0000-\u001f\u007f]/.test(value.model_name)
      && !/https?:|hf_[A-Za-z0-9_-]{16,}/i.test(value.model_name)) result.model_name = value.model_name;
  if (['model', 'python_package', 'runtime'].includes(value.artifact_type)) result.artifact_type = value.artifact_type;
  if (Object.hasOwn(value, 'current_filename')) result.current_filename = safeDisplayFilename(value.current_filename);
  const transferred = byteCount(value.transferred_bytes ?? value.downloaded_bytes ?? value.current);
  const total = byteCount(value.total_bytes ?? value.total);
  const completed = byteCount(value.completed_files ?? value.files_completed);
  const filesTotal = byteCount(value.total_files ?? value.files_total);
  const mode = progressModeFor(value, operation, transferred, total, completed, filesTotal);
  result.progress_mode = mode;
  if (mode === 'bytes') {
    result.transferred_bytes = transferred;
    result.total_bytes = total;
    result.percent = Math.min(100, Math.floor(transferred * 100 / total));
  }
  if (completed !== null && filesTotal !== null && filesTotal > 0) {
    result.completed_files = Math.min(completed, filesTotal);
    result.total_files = filesTotal;
  }
  const expectedSize = byteCount(value.expected_size_bytes);
  if (value.artifact_type === 'model' && expectedSize !== null && expectedSize > 0) result.expected_size_bytes = expectedSize;
  for (const key of ['bytes_per_second', 'speed_bytes_per_second']) {
    if (Object.hasOwn(value, key)) result.bytes_per_second = typeof value[key] === 'number' && Number.isFinite(value[key]) && value[key] > 0
      ? Math.min(value[key], 1_000_000_000_000) : null;
  }
  if (Object.hasOwn(value, 'eta_seconds')) result.eta_seconds = byteCount(value.eta_seconds);
  if (mode !== 'bytes') {
    result.bytes_per_second = null;
    result.eta_seconds = null;
  }
  for (const key of ['transfer_indeterminate', 'indeterminate', 'stalled']) {
    if (Object.hasOwn(value, key)) result[key] = value[key] === true;
  }
  const jobId = safeIdentifier(value.job_id);
  const attemptId = safeIdentifier(value.attempt_id);
  if (jobId) result.job_id = jobId;
  if (attemptId) result.attempt_id = attemptId;
  if (Number.isSafeInteger(value.event_revision) && value.event_revision >= 0) result.event_revision = value.event_revision;
  if (typeof value.message === 'string') result.message = value.message.slice(0, 320)
    .replace(/https?:\/\/\S+/gi, '[link removed]')
    .replace(/hf_[A-Za-z0-9_-]{16,}/g, '[credential removed]')
    .replace(/(?:token|signature|authorization)=\S+/gi, '[credential removed]')
    .replace(/(?:\/home\/|\/Users\/|[A-Za-z]:\\Users\\)[^\s,;)]*/gi, '[local path removed]');
  for (const key of ['stage_id', 'component_id']) {
    if (typeof value[key] === 'string' && /^[a-z][a-z0-9_-]{0,63}$/.test(value[key])) result[key] = value[key];
  }
  if (['reused', 'downloaded', 'installing', 'retrying'].includes(value.cache_status)) result.cache_status = value.cache_status;
  if (Number.isSafeInteger(value.retry_count) && value.retry_count >= 0 && value.retry_count < 100) result.retry_count = value.retry_count;
  if (typeof value.sampled_at === 'string' && /^\d{4}-\d\d-\d\dT/.test(value.sampled_at)) result.sampled_at = value.sampled_at.slice(0, 40);
  return result;
}

function phaseFor(event) {
  if (event.operation && OPERATIONS.has(event.operation)) return event.operation;
  if (event.event === 'resolving') return 'resolving';
  if (event.event === 'retrying') return 'retrying';
  if (event.event === 'reused' || event.event === 'installed') return 'completed';
  if (event.event === 'failed') return 'failed';
  if (event.event === 'verifying' || event.event === 'file_verified') return 'verifying';
  if (event.event === 'publishing' || event.event === 'installed') return 'installing';
  if (['progress', 'downloading', 'source_progress', 'source_acquiring', 'retrying'].includes(event.event)) return 'downloading';
  return null;
}

function smoothRate(samples, now) {
  const fresh = samples.filter((sample) => now - sample.at <= SAMPLE_WINDOW_MS);
  if (fresh.length < 2) return null;
  const elapsed = fresh.at(-1).at - fresh[0].at;
  const delta = fresh.at(-1).bytes - fresh[0].bytes;
  if (elapsed < MIN_SAMPLE_MS || delta <= 0) return null;
  const rate = delta * 1000 / elapsed;
  return Number.isFinite(rate) && rate > 0 ? rate : null;
}

export class ByteTransferSampler {
  constructor({ clock = () => performance.now() } = {}) {
    this.clock = clock;
    this.samples = [];
    this.previousBytes = null;
  }

  update(current, total) {
    if (byteCount(current) === null || byteCount(total) === null || total <= 0 || current > total) {
      return { bytes_per_second: null, eta_seconds: null };
    }
    const now = this.clock();
    if (this.previousBytes !== null && current < this.previousBytes) this.samples = [];
    if (this.previousBytes === null || current > this.previousBytes) this.samples.push({ at: now, bytes: current });
    this.previousBytes = current;
    this.samples = this.samples.filter((sample) => now - sample.at <= SAMPLE_WINDOW_MS);
    const rate = smoothRate(this.samples, now);
    const remaining = total - current;
    return {
      bytes_per_second: rate,
      eta_seconds: rate !== null && remaining > 0 ? Math.ceil(remaining / rate) : null,
    };
  }
}

export class DownloadProgressTracker {
  constructor({ clock = () => performance.now() } = {}) {
    this.clock = clock;
    this.transfer = null;
    this.samples = [];
    this.lastEmission = null;
  }

  reset() {
    this.transfer = null;
    this.samples = [];
    this.lastEmission = null;
  }

  accept(event, role, artifactName) {
    const operation = phaseFor(event);
    if (!operation || !['speech_detection', 'asr', 'diarization', 'speaker_purity'].includes(role)) return null;
    for (const field of ['downloaded_bytes', 'total_bytes', 'file_downloaded_bytes', 'file_total_bytes',
      'files_completed', 'files_total', 'attempt', 'transfer_attempt_bytes', 'model_total_bytes']) {
      if (Object.hasOwn(event, field) && event[field] !== null && byteCount(event[field]) === null) return null;
    }
    const now = this.clock();
    const filename = safeDisplayFilename(event.path || event.current_file);
    const total = byteCount(event.total_bytes);
    const transferred = byteCount(event.downloaded_bytes);
    const completed = byteCount(event.completed_files ?? event.files_completed);
    const filesTotal = byteCount(event.total_files ?? event.files_total);
    const attempt = byteCount(event.attempt);
    const networkBytes = byteCount(event.transfer_attempt_bytes);
    const expectedSize = byteCount(event.model_total_bytes);
    const nameCandidate = typeof event.artifact_name === 'string' ? event.artifact_name : artifactName;
    const name = safeArtifactName(nameCandidate);
    const bytesValid = transferred !== null && total !== null && total > 0 && transferred <= total
      && event.transfer_indeterminate !== true;

    if (operation !== 'downloading') {
      this.reset();
      return {
        operation, phase: operation, role, artifact_name: name, artifact_type: 'model',
        progress_mode: 'indeterminate', current_filename: null,
        ...(expectedSize !== null && expectedSize > 0 ? { expected_size_bytes: expectedSize } : {}),
        ...(event.event === 'reused' ? { cache_status: 'reused' } : {}),
        ...(operation === 'retrying' && Number.isSafeInteger(event.attempt) ? { retry_count: event.attempt } : {}),
      };
    }

    const key = { role, artifact_name: name, filename, attempt };
    const changed = !this.transfer || this.transfer.role !== key.role
      || this.transfer.artifact_name !== key.artifact_name || this.transfer.filename !== key.filename
      || this.transfer.attempt !== key.attempt;
    if (!changed && networkBytes !== null && this.transfer?.networkBytes !== null
        && this.transfer?.networkBytes !== undefined && networkBytes < this.transfer.networkBytes) return null;
    if (changed || event.event === 'retrying' || !bytesValid) this.samples = [];
    const previousNetworkBytes = this.transfer?.networkBytes ?? null;
    if (networkBytes !== null && bytesValid && event.event !== 'retrying') {
      const previous = this.samples.at(-1);
      if (!previous || networkBytes > previous.bytes) this.samples.push({ at: now, bytes: networkBytes });
      this.samples = this.samples.filter((sample) => now - sample.at <= SAMPLE_WINDOW_MS);
    }
    const byteProgressed = networkBytes !== null && (changed || previousNetworkBytes === null || networkBytes > previousNetworkBytes);
    this.transfer = { ...key, networkBytes, lastByteAt: byteProgressed ? now : this.transfer?.lastByteAt ?? now,
      lastEventAt: now, filesCompleted: completed, filesTotal, total, transferred };
    const rate = bytesValid && event.event !== 'retrying' ? smoothRate(this.samples, now) : null;
    const remaining = bytesValid ? total - transferred : null;
    const result = {
      operation: 'downloading', phase: 'downloading', role, artifact_name: name, artifact_type: 'model',
      ...(expectedSize !== null && expectedSize > 0 ? { expected_size_bytes: expectedSize } : {}),
      progress_mode: bytesValid ? 'bytes' : completed !== null && filesTotal !== null && filesTotal > 0 ? 'files' : 'indeterminate',
      ...(bytesValid ? { transferred_bytes: transferred, total_bytes: total,
        percent: Math.min(100, Math.floor(transferred * 100 / total)) } : {}),
      ...(completed !== null && filesTotal !== null ? { completed_files: Math.min(completed, filesTotal), total_files: filesTotal } : {}),
      current_filename: filename,
      bytes_per_second: rate,
      eta_seconds: rate !== null && remaining !== null && remaining > 0 ? Math.ceil(remaining / rate) : null,
      transfer_indeterminate: !bytesValid,
      stalled: false,
    };
    return result;
  }

  stalled() {
    if (!this.transfer || this.transfer.transferred === null || this.transfer.networkBytes === null
        || !this.samples.length || this.clock() - this.transfer.lastByteAt < STALL_AFTER_MS) return null;
    const now = this.clock();
    this.samples = [];
    return {
      operation: 'downloading', phase: 'downloading', role: this.transfer.role,
      artifact_name: this.transfer.artifact_name, artifact_type: 'model', progress_mode: 'bytes',
      transferred_bytes: this.transfer.transferred, total_bytes: this.transfer.total,
      percent: this.transfer.total > 0 ? Math.min(100, Math.floor(this.transfer.transferred * 100 / this.transfer.total)) : null,
      bytes_per_second: null, eta_seconds: null,
      completed_files: this.transfer.filesCompleted, total_files: this.transfer.filesTotal,
      current_filename: this.transfer.filename, transfer_indeterminate: false, stalled: true,
      sampled_at: new Date().toISOString(),
    };
  }

  shouldEmit(progress, { force = false } = {}) {
    const now = this.clock();
    const previous = this.lastEmission;
    const attempt = this.transfer?.attempt ?? null;
    const transferred = byteCount(progress.transferred_bytes);
    const total = byteCount(progress.total_bytes);
    const transferComplete = progress.progress_mode === 'bytes' && transferred !== null && total !== null && total > 0 && transferred === total;
    if (force || !previous || previous.operation !== progress.operation || previous.filename !== progress.current_filename
        || previous.attempt !== attempt || previous.filesCompleted !== progress.completed_files
        || previous.progressMode !== progress.progress_mode || transferComplete) {
      this.lastEmission = { ...previous, at: now, transferred, filename: progress.current_filename,
        attempt, operation: progress.operation, filesCompleted: progress.completed_files, progressMode: progress.progress_mode };
      return true;
    }
    const bytesDelta = transferred !== null && previous.transferred !== null ? transferred - previous.transferred : 0;
    if (now - previous.at >= MIN_EMIT_INTERVAL_MS || bytesDelta >= MIN_EMIT_BYTES) {
      this.lastEmission = { ...previous, at: now, transferred };
      return true;
    }
    return false;
  }
}

export const DOWNLOAD_PROGRESS_LIMITS = Object.freeze({ STALL_AFTER_MS, SAMPLE_WINDOW_MS, MIN_SAMPLE_MS, MIN_EMIT_INTERVAL_MS, MIN_EMIT_BYTES });
