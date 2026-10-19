"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ensureSetupSession, ModelState, RuntimeComponent, SetupApiError, SetupStatus, setupApi, subscribeSetupEvents } from "@/lib/setup-api";
import { ProgressDetails } from "@/lib/setup-progress-view";

const modelTitles: Record<string, string> = {
  speech_detection: "Silero VAD",
  asr: "Whisper Large-v3",
  speaker_purity: "WeSpeaker ECAPA-TDNN512-LM",
  diarization: "pyannote Community-1",
};

const failureGuidance: Record<string, string> = {
  HF_TOKEN_INVALID: "Check the token or create a new Hugging Face read token, then try again.",
  HF_MODEL_ACCESS_REQUIRED: "Open the Community-1 model page, accept its conditions, and retry.",
  HF_TOKEN_PERMISSION_DENIED: "Create a token with read access to gated repositories, then try again.",
  HF_APPROVAL_PENDING: "Hugging Face has not confirmed access yet. Check the model page and retry later.",
  HF_NETWORK_ERROR: "The Hugging Face service could not be reached. Check your connection and retry.",
  HF_RATE_LIMITED: "Hugging Face is temporarily limiting requests. Wait briefly and retry.",
  HF_SNAPSHOT_INCOMPLETE: "Some model files are missing. Retry the installation to repair the snapshot.",
  HF_SNAPSHOT_INTEGRITY_FAILED: "A downloaded model file failed verification. Retry to reacquire it safely.",
  MODEL_STORAGE_INSUFFICIENT: "Free space on the SpeechCraft data drive, then retry.",
  PYANNOTE_RUNTIME_VALIDATION_FAILED: "The model files are installed, but local diarization validation failed. Review diagnostics or retry after repair.",
  AUTHORIZATION_REQUIRED_AFTER_RESTART: "The controller restarted during authorization. Paste your token again to continue.",
};

function titleCase(value: string | null | undefined) {
  if (!value) return "Pending";
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusLabel(component: ModelState | RuntimeComponent | undefined) {
  if (!component) return "Pending";
  const state = component.state || "pending";
  if (state === "runtime_ready" || component.runtime_ready) return "Ready";
  if (state === "files_verified" || state === "verified") return "Files verified";
  if (state === "waiting_for_token" || state === "access_denied" || ("action_required" in component && component.action_required)) return "Action required";
  if (["failed", "error"].includes(state)) return "Failed";
  if (state === "blocked") return "Blocked";
  if (["downloading", "installing", "checking_token", "checking_model_access", "verifying_files", "validating_inference", "starting", "running"].includes(state)) return titleCase(state);
  return titleCase(state);
}

function safeMessage(component: ModelState | RuntimeComponent | undefined) {
  if (!component) return null;
  const code = component.failure_code;
  if (code && failureGuidance[code]) return failureGuidance[code];
  return component.message || null;
}

function StatusDot({ status }: { status: string }) {
  const color = status === "Ready" ? "bg-emerald-500" : status === "Failed" ? "bg-rose-500" : status === "Action required" ? "bg-amber-500" : "bg-[#a8a8a8]";
  return <span aria-hidden="true" className={`inline-block h-2.5 w-2.5 rounded-full ${color}`} />;
}

function ModelRow({ model }: { model: ModelState }) {
  const label = statusLabel(model);
  const message = safeMessage(model);
  return <div className="border-b border-black/10 py-4 last:border-0 dark:border-white/10">
    <div className="flex items-center justify-between gap-4">
      <div className="flex items-center gap-3"><StatusDot status={label} /><div>
        <h3 className="font-medium">{model.display_name || modelTitles[model.role] || model.role}</h3>
        <p className="text-xs text-[#878787]">{model.model_id}</p>
      </div></div>
      <span className="shrink-0 text-sm text-[#777]">{label}</span>
    </div>
    {message && <p className="mt-2 pl-[22px] text-sm text-[#777]">{message}{model.failure_code && <span className="ml-2 font-mono text-xs">{model.failure_code}</span>}</p>}
    <div className="pl-[22px]"><ProgressDetails progress={model.progress} fallbackName={model.display_name || modelTitles[model.role] || model.role} /></div>
    {model.role === "speaker_purity" && <div className="mt-3 ml-[22px] rounded-md border border-black/10 bg-black/[0.02] p-3 text-xs leading-5 text-[#666] dark:border-white/10 dark:bg-white/[0.03] dark:text-[#aaa]" aria-label="ECAPA model attribution and license">
      <p><strong className="font-medium text-[#333] dark:text-[#ddd]">Model attribution:</strong> WeSpeaker ECAPA-TDNN512-LM, published by WeSpeaker. The pinned model file is used unchanged.</p>
      <p>Model revision: <code className="break-all">a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b</code>.</p>
      <p>License: <a className="underline underline-offset-2" href="https://creativecommons.org/licenses/by/4.0/" target="_blank" rel="noreferrer">Creative Commons Attribution 4.0 International (CC BY 4.0)</a>. See the <a className="underline underline-offset-2" href="https://huggingface.co/Wespeaker/wespeaker-ecapa-tdnn512-LM" target="_blank" rel="noreferrer">WeSpeaker model page</a>.</p>
    </div>}
    {model.files_verified && !model.runtime_ready && <p className="mt-1 pl-[22px] text-xs text-[#878787]">Files are verified; runtime inference has not passed yet.</p>}
    {model.release_blockers?.map((blocker) => <p key={blocker} className="mt-1 pl-[22px] text-xs text-amber-700 dark:text-amber-400">Release review: {blocker}</p>)}
  </div>;
}

function RuntimeRow({ name, component }: { name: string; component: RuntimeComponent | undefined }) {
  const label = statusLabel(component);
  const message = safeMessage(component);
  return <div className="flex items-start justify-between gap-4 border-b border-black/10 py-3 last:border-0 dark:border-white/10">
    <div className="flex gap-3"><StatusDot status={label} /><div>
      <p className="font-medium">{name}</p>
      {message && <p className="mt-1 text-sm text-[#777]">{message}</p>}
      {name === "FFmpeg / FFprobe" && <p className="mt-2 max-w-xl text-xs leading-5 text-[#878787]">SpeechCraft obtains FFmpeg and FFprobe from their upstream source for local processing; these executables are not bundled in the application archive. Linux uses a pinned BtbN LGPL build, while macOS builds from pinned FFmpeg source. See the <a className="underline underline-offset-2" href="https://ffmpeg.org/legal.html" target="_blank" rel="noreferrer">FFmpeg license information</a> and the FFmpeg notice included with SpeechCraft.</p>}
      {component?.failure_code && <p className="mt-1 font-mono text-xs text-[#878787]">{component.failure_code}</p>}
    </div></div><span className="shrink-0 text-sm text-[#777]">{label}</span>
  </div>;
}

export default function SetupPage() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [models, setModels] = useState<ModelState[]>([]);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const revision = useRef(-1);
  const mounted = useRef(false);

  const refresh = useCallback(async (isCurrent: () => boolean = () => mounted.current, signal?: AbortSignal) => {
    const [nextStatus, nextModels] = await Promise.all([setupApi.status(signal), setupApi.models(signal)]);
    if (!isCurrent() || nextStatus.state_revision < revision.current) return;
    revision.current = nextStatus.state_revision;
    setStatus(nextStatus);
    setModels(nextModels.models);
    setConnected(true);
    setError(null);
  }, []);

  useEffect(() => {
    mounted.current = true;
    let active = true;
    const requestAbort = new AbortController();
    let closeEvents: (() => void) | undefined;
    let poll: ReturnType<typeof setInterval> | undefined;
    void (async () => {
      try {
        await ensureSetupSession(false, requestAbort.signal);
        if (!active) return;
        await refresh(() => active, requestAbort.signal);
        if (!active) return;
        closeEvents = subscribeSetupEvents((_id, _event, event) => {
          if (!active || event.state_revision < revision.current) return;
          revision.current = event.state_revision;
          void refresh(() => active, requestAbort.signal);
        }, () => { if (active) void refresh(() => active, requestAbort.signal).catch(() => { if (active) setConnected(false); }); }, () => { if (active) setConnected(false); });
        if (!active) { closeEvents(); closeEvents = undefined; return; }
        poll = setInterval(() => { if (active) void refresh(() => active, requestAbort.signal).catch(() => { if (active) setConnected(false); }); }, 15_000);
      } catch (cause) {
        if (!active) return;
        setConnected(false);
        setError(cause instanceof Error ? cause.message : "The local setup controller is unavailable.");
      } finally { if (active) setLoading(false); }
    })();
    return () => { active = false; mounted.current = false; requestAbort.abort(); closeEvents?.(); if (poll) clearInterval(poll); };
  }, [refresh]);

  const runAction = useCallback(async (action: () => Promise<unknown>, success?: string) => {
    setBusy(true);
    setError(null);
    try { await action(); if (success) setError(null); await refresh(); }
    catch (cause) {
      const apiError = cause instanceof SetupApiError ? cause : null;
      const guidance = apiError && failureGuidance[apiError.code];
      setError(guidance ? `${guidance} (${apiError.code})` : cause instanceof Error ? cause.message : "The setup request failed.");
      await refresh().catch(() => setConnected(false));
    } finally { setBusy(false); }
  }, [refresh]);

  const pyannote = useMemo(() => models.find((model) => model.role === "diarization"), [models]);
  const state = status?.job.state || "idle";
  const progress = status?.job.progress;
  const componentMap = status?.components || {};
  const runtimeRows: Array<[string, string]> = [
    ["Managed Python", "managed_python"], ["Backend environment", "backend_environment"],
    ["Worker environment", "worker_environment"], ["FFmpeg / FFprobe", "ffmpeg"],
    ["Backend service", "backend_service"], ["Dataset processing", "dataset_processing"],
  ];
  const actionRequired = pyannote?.action_required === true || pyannote?.state === "waiting_for_token";
  async function submitToken(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || busy) return;
    const submitted = token;
    setBusy(true);
    setError(null);
    try {
      await setupApi.authorizePyannote(submitted);
      setToken("");
      await refresh();
    } catch (cause) {
      const apiError = cause instanceof SetupApiError ? cause : null;
      const guidance = apiError && failureGuidance[apiError.code];
      setError(guidance ? `${guidance} (${apiError.code})` : cause instanceof Error ? cause.message : "Authorization could not be submitted.");
    } finally { setBusy(false); }
  }

  return <main className="mx-auto min-h-screen w-full max-w-3xl px-6 py-10 text-[#262626] dark:text-[#efefef] sm:py-16">
    <header className="mb-8 flex items-start justify-between gap-4">
      <div><p className="mb-2 text-xs uppercase tracking-[0.18em] text-[#878787]">SpeechCraft setup</p>
        <h1 className="font-serif text-3xl">Preparing SpeechCraft</h1>
        <p className="mt-2 max-w-xl text-sm leading-6 text-[#777]">Manage the local runtime and required models. This page reports the setup controller’s current state.</p>
      </div><Link href="/login" className="mt-2 text-sm underline underline-offset-4">Back to SpeechCraft</Link>
    </header>

    <section className="rounded-xl border border-black/10 bg-white/60 px-5 py-4 dark:border-white/10 dark:bg-white/[0.03]" aria-live="polite">
      <div className="flex items-center justify-between gap-4">
        <div><p className="text-xs uppercase tracking-wide text-[#878787]">Setup job</p>
          <p className="mt-1 font-medium">{loading ? "Connecting to the local controller…" : titleCase(state)}</p>
          <p className="mt-1 text-sm text-[#777]">{progress?.message || (status?.simulated ? "Controller simulation mode" : status?.setup_plan || "No setup plan is active")}</p>
          {status?.job.stage && <p className="mt-1 text-xs text-[#878787]">Stage: {titleCase(status.job.stage)}{status.job.task_id ? ` · ${status.job.task_id}` : ""}</p>}
        </div>
        {!connected && <span className="text-xs text-amber-700 dark:text-amber-400">Controller disconnected</span>}
      </div>
      {state === "running" && (progress
        ? <ProgressDetails progress={progress} fallbackName="SpeechCraft setup" />
        : <div className="mt-4 flex items-center gap-2 text-sm text-[#777]"><span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent" />Working; the controller has not reported progress yet.</div>)}
      {status?.mode && <p className="mt-3 text-xs text-[#878787]">Mode: {status.mode}{status.runtime_profile && ` · Profile: ${JSON.stringify(status.runtime_profile)}`}</p>}
      {status?.simulated && <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">Simulation only: this does not install models or validate inference.</p>}
      {status?.job.error && <p role="alert" className="mt-3 text-sm text-rose-700 dark:text-rose-300">{failureGuidance[status.job.error.code] || status.job.error.message} <span className="ml-1 font-mono text-xs">{status.job.error.code}</span></p>}
    </section>

    {error && <div role="alert" className="mt-5 rounded-lg border border-rose-300/70 bg-rose-50 px-4 py-3 text-sm text-rose-900 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200">{error}</div>}

    <section className="mt-8">
      <h2 className="mb-2 font-serif text-xl">Models</h2>
      <div className="rounded-xl border border-black/10 px-5 dark:border-white/10">
        {models.length ? models.map((model) => <ModelRow key={model.role} model={model} />) : <p className="py-5 text-sm text-[#878787]">Model state is not available until the controller responds.</p>}
      </div>
      {actionRequired && <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-50/60 p-5 dark:bg-amber-950/20">
        <h3 className="font-medium">Authorize pyannote Community-1</h3>
        <p className="mt-2 text-sm leading-6 text-[#666] dark:text-[#aaa]">Accept the model conditions, create a Hugging Face read token, then submit it here. SpeechCraft sends the token only to the local controller for this download operation.</p>
        <ol className="mt-3 list-inside list-decimal space-y-1 text-sm">
          <li><a className="underline" href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noreferrer">Accept Community-1 conditions on Hugging Face</a></li>
          <li><a className="underline" href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer">Create a read-access token</a></li>
        </ol>
        <form className="mt-4 flex flex-col gap-3 sm:flex-row" onSubmit={submitToken}>
          <label className="sr-only" htmlFor="hf-token">Hugging Face token</label>
          <input id="hf-token" type="password" autoComplete="off" spellCheck={false} value={token} onChange={(event) => setToken(event.target.value)} placeholder="hf_…" className="min-w-0 flex-1 rounded-md border border-black/15 bg-white px-3 py-2 text-sm outline-none focus:border-black/50 dark:border-white/15 dark:bg-black/20" />
          <button type="submit" disabled={busy || !token} className="rounded-md bg-[#292929] px-4 py-2 text-sm text-white disabled:cursor-not-allowed disabled:opacity-50 dark:bg-[#efefef] dark:text-[#222]">{busy ? "Submitting…" : "Verify & Download"}</button>
        </form>
        {pyannote?.state === "interrupted" && <p className="mt-3 text-sm text-amber-800 dark:text-amber-300">Authorization was interrupted. The token was discarded; paste it again to resume.</p>}
      </div>}
    </section>

    {componentMap.dataset_processing?.blockers && componentMap.dataset_processing.blockers.length > 0 && <section className="mt-6 rounded-lg border border-amber-700/20 bg-amber-50/50 p-4 text-sm dark:bg-amber-950/20">
      <h2 className="font-medium">What is blocking processing</h2>
      <ul className="mt-2 list-disc space-y-1 pl-5">{componentMap.dataset_processing.blockers.map((blocker) => <li key={`${blocker.component}:${blocker.code}`}>{blocker.message} <span className="font-mono text-xs">{blocker.code}</span></li>)}</ul>
    </section>}

    <section className="mt-8">
      <h2 className="mb-2 font-serif text-xl">Runtime</h2>
      <div className="rounded-xl border border-black/10 px-5 dark:border-white/10">
        {runtimeRows.map(([name, key]) => <RuntimeRow key={key} name={name} component={componentMap[key]} />)}
      </div>
    </section>

    {status?.production_ready === false && !status.simulated && <p className="mt-5 text-sm text-[#777]">Public release readiness is separate from local technical readiness. Licensing, provenance or authorization blockers may remain after local components work.</p>}
    {status?.technical_processing_ready && <div className="mt-6 rounded-lg border border-emerald-700/20 bg-emerald-50/50 p-4 text-sm dark:bg-emerald-950/20">
      <p className="font-medium">Local processing prerequisites are ready.</p><p className="mt-1 text-[#777]">This does not clear public-release approvals.</p>
      <Link href="/login" className="mt-3 inline-block underline underline-offset-4">Continue to SpeechCraft</Link>
    </div>}
    {!status?.technical_processing_ready && status && state === "completed" && <p className="mt-5 text-sm text-[#777]">The setup plan completed, but the controller has not marked dataset processing ready. Review the component states above.</p>}

    <div className="mt-8 flex flex-wrap gap-3">
      {state === "idle" && <button disabled={busy || !connected} onClick={() => void runAction(setupApi.start)} className="rounded-md bg-[#292929] px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-[#efefef] dark:text-[#222]">Prepare SpeechCraft</button>}
      {["running", "waiting_for_user"].includes(state) && <button disabled={busy} onClick={() => void runAction(setupApi.cancel)} className="rounded-md border border-black/20 px-4 py-2 text-sm disabled:opacity-50 dark:border-white/20">Cancel setup</button>}
      {["failed", "cancelled", "interrupted"].includes(state) && <button disabled={busy} onClick={() => void runAction(setupApi.retry)} className="rounded-md bg-[#292929] px-4 py-2 text-sm text-white disabled:opacity-50 dark:bg-[#efefef] dark:text-[#222]">Retry setup</button>}
      {["completed", "failed", "cancelled", "interrupted"].includes(state) && <button disabled={busy} onClick={() => void runAction(setupApi.repair)} className="rounded-md border border-black/20 px-4 py-2 text-sm disabled:opacity-50 dark:border-white/20">Repair / recheck</button>}
      {!connected && <button disabled={busy} onClick={() => void runAction(async () => { await ensureSetupSession(true); await refresh(); })} className="rounded-md border border-black/20 px-4 py-2 text-sm disabled:opacity-50 dark:border-white/20">Reconnect</button>}
    </div>
    <footer className="mt-10 border-t border-black/10 pt-4 text-xs text-[#878787] dark:border-white/10">Job completion reports the selected setup plan only. Production readiness is shown separately by the controller.</footer>
  </main>;
}
