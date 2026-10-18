import { performance } from 'node:perf_hooks';

const MAX_SAFE_BYTES = Number.MAX_SAFE_INTEGER;
const STALL_AFTER_MS = 3_000;
const SAMPLE_WINDOW_MS = 5_000;
const MIN_SAMPLE_MS = 250;
const MIN_EMIT_INTERVAL_MS = 750;
const MIN_EMIT_BYTES = 1024 * 1024;
const SETUP_PHASES = new Set([
  'queued', 'inspecting', 'downloading', 'verifying', 'verified', 'installing',
  'running_inference', 'interrupted', 'complete',
]);

function byteCount(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= MAX_SAFE_BYTES ? value : null;
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

export function sanitizeProgressTelemetry(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const roles = new Set(['speech_detection', 'asr', 'diarization', 'speaker_purity']);
  const result = {};
  if (SETUP_PHASES.has(value.phase)) result.phase = value.phase;
  if (roles.has(value.role)) result.role = value.role;
  for (const key of ['model_name']) {
    if (typeof value[key] === 'string' && value[key].length <= 100 && !/[\u0000-\u001f\u007f]/.test(value[key])
        && !/https?:|hf_[A-Za-z0-9_-]{16,}/i.test(value[key])) result[key] = value[key];
  }
  if (Object.hasOwn(value, 'current_filename')) result.current_filename = safeDisplayFilename(value.current_filename);
  for (const key of ['downloaded_bytes', 'total_bytes', 'files_completed', 'files_total', 'current', 'total']) {
    if (Object.hasOwn(value, key)) result[key] = byteCount(value[key]);
  }
  for (const key of ['bytes_per_second', 'speed_bytes_per_second']) {
    if (Object.hasOwn(value, key)) result[key] = typeof value[key] === 'number' && Number.isFinite(value[key]) && value[key] > 0
      ? Math.min(value[key], 1_000_000_000_000) : null;
  }
  for (const key of ['eta_seconds']) {
    if (Object.hasOwn(value, key)) result[key] = byteCount(value[key]);
  }
  if (Object.hasOwn(value, 'percent')) result.percent = typeof value.percent === 'number' && Number.isFinite(value.percent)
    ? Math.max(0, Math.min(100, value.percent)) : null;
  for (const key of ['transfer_indeterminate', 'indeterminate', 'stalled']) {
    if (Object.hasOwn(value, key)) result[key] = value[key] === true;
  }
  if (typeof value.message === 'string') result.message = value.message.slice(0, 320)
    .replace(/https?:\/\/\S+/gi, '[link removed]')
    .replace(/hf_[A-Za-z0-9_-]{16,}/g, '[credential removed]')
    .replace(/(?:token|signature|authorization)=\S+/gi, '[credential removed]');
  if (typeof value.sampled_at === 'string' && /^\d{4}-\d\d-\d\dT/.test(value.sampled_at)) result.sampled_at = value.sampled_at.slice(0, 40);
  return result;
}

function phaseFor(event) {
  if (event.event === 'verifying') return 'verifying';
  if (event.event === 'publishing' || event.event === 'installed') return 'installing';
  if (['progress', 'downloading', 'source_progress', 'source_file_acquired', 'file_verified', 'source_acquiring'].includes(event.event)) return 'downloading';
  if (event.event === 'retrying') return 'downloading';
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

  accept(event, role, modelName) {
    const phase = phaseFor(event);
    if (!phase || !['speech_detection', 'asr', 'diarization', 'speaker_purity'].includes(role)) return null;
    for (const field of ['downloaded_bytes', 'total_bytes', 'files_completed', 'files_total', 'attempt', 'transfer_attempt_bytes']) {
      if (Object.hasOwn(event, field) && event[field] !== null && byteCount(event[field]) === null) return null;
    }
    const now = this.clock();
    const filename = safeDisplayFilename(event.path || event.current_file);
    const total = byteCount(event.total_bytes);
    const rawDownloaded = byteCount(event.downloaded_bytes);
    const downloaded = rawDownloaded !== null && total !== null ? Math.min(rawDownloaded, total) : rawDownloaded;
    const rawFilesCompleted = byteCount(event.files_completed);
    const filesTotal = byteCount(event.files_total);
    const filesCompleted = rawFilesCompleted !== null && filesTotal !== null
      ? Math.min(rawFilesCompleted, filesTotal) : rawFilesCompleted;
    const attempt = byteCount(event.attempt);
    const networkBytes = byteCount(event.transfer_attempt_bytes);
    const indeterminate = event.transfer_indeterminate === true || downloaded === null;

    if (phase !== 'downloading') {
      this.reset();
      return {
        phase, role, model_name: modelName, current_filename: null,
        downloaded_bytes: null, total_bytes: total,
        bytes_per_second: null, eta_seconds: null,
        files_completed: filesCompleted, files_total: filesTotal,
        transfer_indeterminate: true, stalled: false,
      };
    }

    const key = { role, filename, attempt };
    const changed = !this.transfer || this.transfer.role !== key.role
      || this.transfer.filename !== key.filename || this.transfer.attempt !== key.attempt;
    if (!changed && networkBytes !== null && this.transfer?.networkBytes !== null
        && this.transfer?.networkBytes !== undefined && networkBytes < this.transfer.networkBytes) return null;
    if (changed || event.event === 'retrying' || networkBytes === null) {
      this.samples = [];
    }
    const previousNetworkBytes = this.transfer?.networkBytes ?? null;
    if (networkBytes !== null && !indeterminate) {
      const previous = this.samples.at(-1);
      if (!previous || networkBytes > previous.bytes) this.samples.push({ at: now, bytes: networkBytes });
      this.samples = this.samples.filter((sample) => now - sample.at <= SAMPLE_WINDOW_MS);
    }
    const byteProgressed = networkBytes !== null && (changed || previousNetworkBytes === null || networkBytes > previousNetworkBytes);
    this.transfer = { ...key, networkBytes, lastByteAt: byteProgressed ? now : this.transfer?.lastByteAt ?? now,
      lastEventAt: now, filesCompleted, filesTotal, total, downloaded, modelName };
    const rate = !indeterminate && event.event !== 'retrying' ? smoothRate(this.samples, now) : null;
    const remaining = downloaded !== null && total !== null ? Math.max(0, total - downloaded) : null;
    const eta = rate !== null && remaining !== null && remaining > 0 ? Math.ceil(remaining / rate) : null;
    const result = {
      phase, role, model_name: modelName, current_filename: filename,
      downloaded_bytes: downloaded, total_bytes: total,
      bytes_per_second: rate, eta_seconds: eta,
      files_completed: filesCompleted, files_total: filesTotal,
      transfer_indeterminate: indeterminate, stalled: false,
    };
    return result;
  }

  stalled() {
    if (!this.transfer || this.transfer.downloaded === null || this.transfer.networkBytes === null
        || !this.samples.length || this.clock() - this.transfer.lastByteAt < STALL_AFTER_MS) return null;
    const now = this.clock();
    this.samples = [];
    return {
      phase: 'downloading', role: this.transfer.role,
      current_filename: this.transfer.filename,
      downloaded_bytes: this.transfer.downloaded, total_bytes: this.transfer.total,
      bytes_per_second: null, eta_seconds: null,
      files_completed: this.transfer.filesCompleted, files_total: this.transfer.filesTotal,
      transfer_indeterminate: false, stalled: true,
      model_name: this.transfer.modelName,
      sampled_at: new Date().toISOString(),
    };
  }

  shouldEmit(progress, { force = false } = {}) {
    const now = this.clock();
    const previous = this.lastEmission;
    const attempt = this.transfer?.attempt ?? null;
    const downloaded = byteCount(progress.downloaded_bytes);
    const total = byteCount(progress.total_bytes);
    const transferComplete = downloaded !== null && total !== null && total > 0 && downloaded >= total;
    if (force || !previous || previous.phase !== progress.phase || previous.filename !== progress.current_filename
        || previous.attempt !== attempt || previous.filesCompleted !== progress.files_completed
        || transferComplete) {
      this.lastEmission = { ...previous, at: now, downloaded: progress.downloaded_bytes,
        filename: progress.current_filename, attempt, phase: progress.phase, filesCompleted: progress.files_completed };
      return true;
    }
    const bytesDelta = progress.downloaded_bytes !== null && previous.downloaded !== null
      ? progress.downloaded_bytes - previous.downloaded : 0;
    if (now - previous.at >= MIN_EMIT_INTERVAL_MS || bytesDelta >= MIN_EMIT_BYTES) {
      this.lastEmission = { ...previous, at: now, downloaded: progress.downloaded_bytes };
      return true;
    }
    return false;
  }
}

export const DOWNLOAD_PROGRESS_LIMITS = Object.freeze({ STALL_AFTER_MS, SAMPLE_WINDOW_MS, MIN_SAMPLE_MS, MIN_EMIT_INTERVAL_MS, MIN_EMIT_BYTES });
