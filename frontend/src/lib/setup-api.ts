export type SetupError = { code: string; message: string };
export type SetupJob = {
  id: string | null;
  state: string;
  stage: string | null;
  task_id?: string | null;
  progress?: { phase?: string; current?: number | null; total?: number | null; percent?: number | null; message?: string; indeterminate?: boolean } | null;
  error?: SetupError | null;
  attempt_id?: string | null;
  attempt_count?: number;
};
export type RuntimeComponent = {
  identity?: string;
  state?: string;
  verification?: string;
  last_verified_at?: string | null;
  failure_code?: string | null;
  message?: string | null;
  runtime_ready?: boolean;
  progress?: { percent?: number | null; message?: string } | null;
  progress_indeterminate?: boolean;
  device?: string | null;
  effective_profile?: string | null;
  release_blockers?: Array<{ code?: string; message?: string }>;
  blockers?: Array<{ component: string; code: string; message: string }>;
};
export type SetupStatus = {
  controller_status: string;
  job: SetupJob;
  state_revision: number;
  mode: string;
  simulated: boolean;
  setup_plan: string | null;
  components: Record<string, RuntimeComponent> | null;
  runtime_profile: Record<string, unknown> | null;
  technical_processing_ready: boolean;
  bootstrap_ready: boolean;
  partial_ready: boolean;
  production_ready: boolean;
  onboarding: { completed: boolean; completed_at?: string | null; profile?: string | null };
};
export type ModelState = {
  role: string;
  model_id: string;
  display_name: string;
  state: string;
  progress: { percent?: number | null; message?: string } | null;
  files_verified: boolean;
  runtime_ready: boolean;
  action_required: boolean;
  failure_code: string | null;
  message: string | null;
  release_blockers: string[];
};
export type ModelList = { schema_version: number; models: ModelState[] };
export type SetupSnapshot = {
  controller_status: string;
  state_revision: number;
  job: SetupJob;
  mode: string;
  simulated: boolean;
  components: SetupStatus["components"];
  technical_processing_ready: boolean;
  production_ready: boolean;
  onboarding: SetupStatus["onboarding"];
};

let csrfToken: string | null = null;
let acquiringSession: Promise<void> | null = null;

export class SetupApiError extends Error {
  code: string;
  status: number;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "SetupApiError";
    this.status = status;
    this.code = code;
  }
}

async function responseError(response: Response): Promise<SetupApiError> {
  let body: { error?: { code?: string; message?: string } } = {};
  try { body = await response.json(); } catch { /* use a safe generic message */ }
  return new SetupApiError(response.status, body.error?.code || "SETUP_REQUEST_FAILED",
    body.error?.message || "SpeechCraft could not complete that setup request.");
}

async function getSession(signal?: AbortSignal): Promise<void> {
  const current = await fetch("/setup-api/session", { credentials: "same-origin", cache: "no-store", signal });
  if (current.ok) {
    const result = await current.json() as { csrf_token?: string };
    if (typeof result.csrf_token !== "string") throw new SetupApiError(502, "SESSION_RESPONSE_INVALID", "The setup session response was incomplete.");
    csrfToken = result.csrf_token;
    return;
  }
  if (current.status !== 401) throw await responseError(current);
  const established = await fetch("/setup-api/session", { method: "POST", credentials: "same-origin", cache: "no-store",
    headers: { "Content-Type": "application/json" }, body: "{}", signal });
  if (!established.ok) throw await responseError(established);
  const result = await established.json() as { csrf_token?: string };
  if (typeof result.csrf_token !== "string") throw new SetupApiError(502, "SESSION_RESPONSE_INVALID", "The setup session response was incomplete.");
  csrfToken = result.csrf_token;
}

export async function ensureSetupSession(force = false, signal?: AbortSignal): Promise<void> {
  if (force) csrfToken = null;
  if (csrfToken) return;
  if (signal) return getSession(signal);
  if (!acquiringSession) acquiringSession = getSession().finally(() => { acquiringSession = null; });
  return acquiringSession;
}

async function request<T>(route: string, init: RequestInit = {}, retrySession = true): Promise<T> {
  const method = init.method || "GET";
  if (method !== "GET") await ensureSetupSession(false, init.signal || undefined);
  const headers = new Headers(init.headers);
  if (method !== "GET") {
    headers.set("Content-Type", "application/json");
    if (csrfToken) headers.set("X-SpeechCraft-CSRF", csrfToken);
  }
  const response = await fetch(`/setup-api/${route}`, { ...init, headers, credentials: "same-origin", cache: "no-store" });
  if (response.status === 401 && retrySession) {
    await ensureSetupSession(true, init.signal || undefined);
    return request<T>(route, init, false);
  }
  if (!response.ok) throw await responseError(response);
  return response.json() as Promise<T>;
}

export const setupApi = {
  ensureSession: ensureSetupSession,
  status: (signal?: AbortSignal) => request<SetupStatus>("status", { signal }),
  models: (signal?: AbortSignal) => request<ModelList>("models", { signal }),
  start: () => request("start", { method: "POST", body: "{}" }),
  retry: () => request("retry", { method: "POST", body: "{}" }),
  repair: () => request("repair", { method: "POST", body: "{}" }),
  cancel: () => request("cancel", { method: "POST", body: "{}" }),
  authorizePyannote: (token: string) => request("models/pyannote/authorize", {
    method: "POST", body: JSON.stringify({ token }),
  }),
};

export function subscribeSetupEvents(onEvent: (id: number | null, event: string, data: SetupSnapshot) => void,
  onReconnect: () => void, onError: () => void) {
  const abort = new AbortController();
  let lastId: number | null = null;
  let stopped = false;
  const run = async () => {
    let delay = 500;
    while (!stopped) {
      try {
        await ensureSetupSession(false, abort.signal);
        const headers = new Headers({ Accept: "text/event-stream" });
        if (lastId !== null) headers.set("Last-Event-ID", String(lastId));
        const response = await fetch("/setup-api/events", { headers, credentials: "same-origin", cache: "no-store", signal: abort.signal });
        if (response.status === 401) { await ensureSetupSession(true, abort.signal); continue; }
        if (!response.ok || !response.body) throw new Error("SSE unavailable");
        onReconnect();
        delay = 500;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() || "";
          for (const frame of frames) {
            let id: number | null = null;
            let event = "message";
            let data = "";
            for (const line of frame.split(/\r?\n/)) {
              if (line.startsWith("id: ")) id = Number(line.slice(4));
              else if (line.startsWith("event: ")) event = line.slice(7);
              else if (line.startsWith("data: ")) data += line.slice(6);
            }
            if (!data) continue;
            try {
              const parsed = JSON.parse(data) as SetupSnapshot;
              const revision = id ?? parsed.state_revision;
              if (Number.isSafeInteger(revision) && (lastId === null || revision >= lastId)) {
                lastId = revision;
                onEvent(id, event, parsed);
              }
            } catch { /* ignore malformed frames and refetch on the next valid snapshot */ }
          }
        }
      } catch {
        if (stopped) break;
        onError();
      }
      if (!stopped) await new Promise((resolve) => setTimeout(resolve, delay));
      delay = Math.min(delay * 2, 5000);
    }
  };
  void run();
  return () => { stopped = true; abort.abort(); };
}
