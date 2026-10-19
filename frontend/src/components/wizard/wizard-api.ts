// Wizard-side client for the live FastAPI backend (same-origin via /sc-api).
// Covers project creation + streamed recording upload; run/diarization/
// processing calls are added as their wizard steps are wired.

import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import { speechcraftApiBase } from "@/lib/api-base";
import {
  WIZARD_SELECTED_SPEAKER_STOP_AFTER,
  wizardResumeOptions,
  wizardRunOptions,
} from "./processing-plan.mjs";

const BASE = speechcraftApiBase();

export type SpeechcraftProject = {
  id: string;
  name: string;
};

export type SourceRecording = {
  id: string;
  display_name?: string | null;
  duration_sec?: number | null;
  sample_rate_hz?: number | null;
  channels?: number | null;
};

const UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024;

async function parseError(res: Response): Promise<SpeechcraftApiError> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const body = await res.json();
    if (body && typeof body === "object" && "detail" in body) {
      const d = (body as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    }
  } catch {
    /* keep status line */
  }
  return new SpeechcraftApiError(res.status, detail);
}

/** Create a project (import batch). id must be url-safe and unique. */
export async function createProject(
  id: string,
  name: string,
): Promise<SpeechcraftProject> {
  const res = await fetch(`${BASE}/api/import-batches`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ id, name }),
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as SpeechcraftProject;
}

/**
 * Upload bounded, independently verified chunks. Progress advances only after
 * the backend has committed each chunk; 100% means validation and conversion
 * have also completed.
 */
export async function uploadRecording(
  projectId: string,
  file: File,
  onProgress: (pct: number) => void,
): Promise<SourceRecording> {
  const root = `${BASE}/api/projects/${encodeURIComponent(projectId)}/source-recordings/upload-sessions`;
  const chunkHashes: string[] = [];
  for (let offset = 0; offset < file.size; offset += UPLOAD_CHUNK_SIZE) {
    const chunk = file.slice(offset, Math.min(file.size, offset + UPLOAD_CHUNK_SIZE));
    const digest = await crypto.subtle.digest("SHA-256", await chunk.arrayBuffer());
    chunkHashes.push(Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join(""));
  }
  const identityManifest = new Uint8Array(8 + chunkHashes.length * 32);
  new DataView(identityManifest.buffer).setBigUint64(0, BigInt(file.size));
  chunkHashes.forEach((hex, index) => {
    for (let byte = 0; byte < 32; byte++) {
      identityManifest[8 + index * 32 + byte] = Number.parseInt(hex.slice(byte * 2, byte * 2 + 2), 16);
    }
  });
  const identityDigest = await crypto.subtle.digest("SHA-256", identityManifest);
  const fileIdentity = Array.from(new Uint8Array(identityDigest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  const key = `speechcraft-upload:${projectId}:${file.name}:${file.size}:${file.lastModified}:${fileIdentity}`;
  const clientKey = `${key}:client`;
  const request = async <T>(url: string, init?: RequestInit): Promise<T> => {
    const response = await fetch(url, { ...init, headers: { Accept: "application/json", ...init?.headers } });
    if (!response.ok) throw await parseError(response);
    return (await response.json()) as T;
  };
  const retry = async <T>(operation: () => Promise<T>): Promise<T> => {
    let last: unknown;
    for (let attempt = 0; attempt < 5; attempt++) {
      try { return await operation(); } catch (error) {
        last = error;
        const retryable = !(error instanceof SpeechcraftApiError) || error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500;
        if (!retryable || attempt === 4) throw error;
        await new Promise((resolve) => setTimeout(resolve, 400 * 2 ** attempt));
      }
    }
    throw last;
  };
  type UploadStatus = { upload_id: string; chunk_size: number; chunk_count: number; committed_bytes: number; completed_chunks: number[]; completed?: boolean; recording_id?: string | null; file_identity_sha256: string };
  let uploadId = "";
  let clientUploadId = "";
  try { uploadId = localStorage.getItem(key) || ""; } catch { /* private browsing can disable storage */ }
  try { clientUploadId = localStorage.getItem(clientKey) || ""; } catch { /* private browsing can disable storage */ }
  if (!/^[0-9a-f]{32}$/.test(clientUploadId)) {
    clientUploadId = crypto.randomUUID().replaceAll("-", "");
    try { localStorage.setItem(clientKey, clientUploadId); } catch { /* request retries still reuse this id */ }
  }
  let status: UploadStatus | null = null;
  if (uploadId) {
    try {
      status = await request<UploadStatus>(`${root}/${encodeURIComponent(uploadId)}?file_identity_sha256=${fileIdentity}`);
    } catch (error) {
      uploadId = "";
      if (error instanceof SpeechcraftApiError && error.status === 409) {
        clientUploadId = crypto.randomUUID().replaceAll("-", "");
        try { localStorage.setItem(clientKey, clientUploadId); } catch { /* request retries still reuse this id */ }
      }
    }
  }
  if (!uploadId) {
    status = await retry(() => request<UploadStatus>(root, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name, size: file.size, client_upload_id: clientUploadId,
        file_identity_sha256: fileIdentity, chunk_hashes: chunkHashes,
      }),
    }));
    uploadId = status.upload_id;
    try { localStorage.setItem(key, uploadId); } catch { /* resume remains available for this page */ }
  }
  if (status?.completed && status.recording_id) {
    const recording = (await request<SourceRecording[]>(`${BASE}/api/projects/${encodeURIComponent(projectId)}/source-recordings`))
      .find((item) => item.id === status?.recording_id);
    if (recording) {
      try { localStorage.removeItem(key); localStorage.removeItem(clientKey); } catch { /* ignore */ }
      onProgress(100);
      return recording;
    }
  }
  const chunkSize = status!.chunk_size;
  const completed = new Set(status!.completed_chunks);
  onProgress(Math.min(99, Math.floor((status!.committed_bytes / file.size) * 99)));
  const url = `${root}/${encodeURIComponent(uploadId)}`;
  for (let index = 0; index < status!.chunk_count; index++) {
    if (completed.has(index)) continue;
    const chunk = file.slice(index * chunkSize, Math.min(file.size, (index + 1) * chunkSize));
    const digest = chunkHashes[index]!;
    await retry(() => request<UploadStatus>(`${url}/chunks/${index}`, {
      method: "PUT", headers: { "Content-Type": "application/octet-stream", "X-Chunk-SHA256": digest }, body: chunk,
    }));
    completed.add(index);
    const committed = Math.min(file.size, completed.size * chunkSize);
    onProgress(Math.min(99, Math.floor((committed / file.size) * 99)));
  }
  const recording = await retry(() => request<SourceRecording>(`${url}/finalize`, { method: "POST" }));
  try { localStorage.removeItem(key); localStorage.removeItem(clientKey); } catch { /* ignore */ }
  onProgress(100);
  return recording;
}

/** Build a url-safe, reasonably-unique project id from a dataset name. */
export function makeProjectId(name: string): string {
  const slug =
    name
      .toLowerCase()
      .replace(/\.[a-z0-9]+$/i, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 24) || "dataset";
  const rand = Math.random().toString(36).slice(2, 8);
  return `${slug}-${rand}`;
}

// ── Run lifecycle ─────────────────────────────────────────────────────────

export type WhisperModelSize = "large-v3" | "base";

export type DatasetRunView = {
  id: string;
  project_id: string;
  stage: string;
  status: string; // pending | running | completed | needs_review | rejected | failed
  input_summary: Record<string, unknown>;
  output_summary: Record<string, unknown>;
  reason_codes: string[];
};

export type PreflightResult = {
  ok: boolean;
  error?: string;
  reason_codes?: string[];
  [k: string]: unknown;
};

export type SpeakerSample = {
  sample_id: string;
  speaker_id: string;
  source_audio_id: string;
  audio_path: string;
  duration_sec: number;
};

export type SpeakerSelection = {
  mode: string;
  selected: boolean;
  target_speaker_id: string | null;
  available_speaker_ids: string[];
};

export type SpeakerResults = {
  run_id: string;
  speaker_regions_summary: { speaker_ids?: string[] } & Record<string, unknown>;
  speaker_samples_manifest: SpeakerSample[];
  speaker_selection: SpeakerSelection | null;
};

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

async function sendJson<T>(path: string, method: "POST" | "PUT", body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

/** Same-origin media URL for a run artifact / sample audio. */
export function scMediaUrl(pathOrUrl: string | null | undefined): string | null {
  if (!pathOrUrl) return null;
  if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://")) return pathOrUrl;
  return `${BASE}${pathOrUrl}`;
}

export function speakerSampleUrl(runId: string, sampleId: string): string {
  return `${BASE}/media/dataset-runs/${runId}/speaker-samples/${sampleId}.wav`;
}

/** Capability check — used as the hardware/model resolver. */
export function fetchPreflight(asrModel?: string): Promise<PreflightResult> {
  const q = asrModel ? `?asr_model=${encodeURIComponent(asrModel)}` : "";
  return getJson<PreflightResult>(`/api/system/preflight${q}`);
}

/**
 * Resolve the most capable whisper model the hardware can actually load:
 * prefer large-v3, fall back to base, else the run cannot proceed (error).
 */
export async function resolveWhisperModel(): Promise<
  { model: WhisperModelSize } | { model: null; error: string }
> {
  const large = await fetchPreflight("large-v3");
  if (large.ok) return { model: "large-v3" };
  const base = await fetchPreflight("base");
  if (base.ok) return { model: "base" };
  return {
    model: null,
    error: base.error || large.error || "No usable Whisper model for this hardware.",
  };
}

/** Create a diarization-mode run (single_speaker=false so we can pick the target). */
export function createDatasetRun(
  projectId: string,
  body: {
    source_recording_ids?: string[];
    whisper_model_size: WhisperModelSize;
    language?: string;
    stop_after?: string;
    single_speaker?: boolean;
    config?: Record<string, unknown>;
  },
): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/projects/${projectId}/dataset-runs`, "POST", {
    source_recording_ids: body.source_recording_ids ?? [],
    config: body.config ?? {},
    single_speaker: body.single_speaker ?? false, // diarize; output stays single-speaker via selection
    target_speaker_label: "speaker_0",
    language: body.language ?? "auto",
    whisper_model_size: body.whisper_model_size,
    ...wizardRunOptions(body.stop_after),
  });
}

export function startRun(runId: string): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/dataset-runs/${runId}/start`, "POST", {});
}

export function fetchRun(runId: string): Promise<DatasetRunView> {
  return getJson<DatasetRunView>(`/api/dataset-runs/${runId}`);
}

export function refreshRun(runId: string): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/dataset-runs/${runId}/refresh`, "POST", {});
}

export function fetchSpeakers(runId: string): Promise<SpeakerResults> {
  return getJson<SpeakerResults>(`/api/dataset-runs/${runId}/speakers`);
}

export function saveSpeakerSelection(
  runId: string,
  targetSpeakerId: string,
): Promise<SpeakerSelection> {
  return sendJson<SpeakerSelection>(`/api/dataset-runs/${runId}/speaker-selection`, "PUT", {
    target_speaker_id: targetSpeakerId,
  });
}

export function resumeProcessing(
  runId: string,
  stopAfter: "buffers" | "candidate_review_clips" | "transcript_qc" | "speaker_purity" | "native_export" = WIZARD_SELECTED_SPEAKER_STOP_AFTER,
): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/dataset-runs/${runId}/resume-processing`, "POST", {
    ...wizardResumeOptions(stopAfter),
  });
}

export function slicerRerun(
  runId: string,
  config: Record<string, number> = {},
): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/dataset-runs/${runId}/slicer-rerun`, "POST", { config });
}

export function generateQc(runId: string, force = false): Promise<DatasetRunView> {
  return sendJson<DatasetRunView>(`/api/dataset-runs/${runId}/qc/generate`, "POST", { force });
}

/** Distinct diarized speaker ids from a speakers payload. */
export function distinctSpeakerIds(results: SpeakerResults): string[] {
  const fromSummary = results.speaker_regions_summary?.speaker_ids ?? [];
  const fromSamples = results.speaker_samples_manifest.map((s) => s.speaker_id);
  return Array.from(new Set([...fromSummary, ...fromSamples])).sort();
}

/**
 * Poll a run (via refresh, which advances worker status) until `predicate`
 * holds. Throws on run failure or timeout. Honors an AbortSignal so callers
 * can cancel on unmount.
 */
export async function waitForRun(
  runId: string,
  predicate: (run: DatasetRunView) => boolean,
  opts?: {
    intervalMs?: number;
    timeoutMs?: number;
    signal?: AbortSignal;
    onTick?: (run: DatasetRunView) => void;
  },
): Promise<DatasetRunView> {
  const interval = opts?.intervalMs ?? 2500;
  const timeout = opts?.timeoutMs ?? 3_600_000; // 1h ceiling
  const start = Date.now();
  for (;;) {
    if (opts?.signal?.aborted) throw new SpeechcraftApiError(0, "cancelled");
    const run = await refreshRun(runId);
    opts?.onTick?.(run);
    if (predicate(run)) return run;
    if (run.status === "failed" || run.status === "rejected") {
      throw new SpeechcraftApiError(0, `Run ${run.status} at stage "${run.stage}"`);
    }
    if (Date.now() - start > timeout) {
      throw new SpeechcraftApiError(0, "Timed out waiting for the run");
    }
    await new Promise((r) => setTimeout(r, interval));
  }
}
