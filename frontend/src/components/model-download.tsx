"use client";

import { SubmitButton } from "@midday/ui/submit-button";
import { Spinner } from "@midday/ui/spinner";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import {
  type AsrEnsureJob,
  type AsrEnsureProgress,
  type AsrAvailability,
  type WhisperModelSize,
  fetchAsrEnsure,
  resolveWhisperModel,
  startAsrEnsure,
} from "./wizard/wizard-api";
import { demoEnabled, withDemo } from "@/lib/demo";
import {
  type ModelState,
  type ProgressTelemetry,
  SetupApiError,
  type SetupStatus,
  setupApi,
  isLauncherManagedInstallation,
  subscribeSetupEvents,
} from "@/lib/setup-api";
import { downloadPercent, latestProgressForJob, setupActionFor, setupFailureCodeFor, setupFailureFor } from "@/lib/model-onboarding.mjs";

type Phase = "checking" | "needs_authorization" | "access_verified" | "needs_download" | "downloading" | "error";
type ErrorView = { category: string; message: string; action: "retry" | "verify" };
type ProgressView = { title: string; artifact: string; kind: "Model" | "Python package" | "Runtime"; progress: ProgressTelemetry | AsrEnsureProgress | null };

const ENSURE_JOB_PREFIX = "speechcraft-login3-ensure:";

function ensureStorageKey(model: WhisperModelSize) { return `${ENSURE_JOB_PREFIX}${model}`; }
function readEnsureJobId(model: WhisperModelSize) {
  try { return localStorage.getItem(ensureStorageKey(model)); } catch { return null; }
}
function storeEnsureJobId(model: WhisperModelSize, id: string | null) {
  try {
    if (id) localStorage.setItem(ensureStorageKey(model), id);
    else localStorage.removeItem(ensureStorageKey(model));
  } catch { /* optional recovery hint only */ }
}

function formatBytes(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value < 1_000) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value;
  let index = -1;
  do { amount /= 1_000; index += 1; } while (amount >= 1_000 && index < units.length - 1);
  return `${amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatEta(seconds: number | null | undefined) {
  if (!Number.isSafeInteger(seconds) || seconds! < 0) return null;
  if (seconds === 0) return "Less than a second";
  if (seconds! < 60) return `About ${seconds} sec left`;
  const minutes = Math.ceil(seconds! / 60);
  if (minutes < 60) return `About ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `About ${hours} hr ${rest} min left` : `About ${hours} hr left`;
}

function setupProgress(status: SetupStatus, models: ModelState[]): ProgressView {
  const progress = latestProgressForJob(status.job.id, status.job.attempt_id,
    [status.job.progress, ...models.map((item) => item.progress)]);
  const stage = status.job.stage || "";
  const packageStage = /install_(model_manager|backend_environment|worker_environment)/.test(stage);
  const kind = progress?.artifact_type === "model" ? "Model"
    : progress?.artifact_type === "python_package" ? "Python package"
      : progress?.artifact_type === "runtime" ? "Runtime"
        : packageStage ? "Python package" : stage === "install_models" ? "Model" : "Runtime";
  const operation = progress?.operation || "checking";
  const stageArtifact: Record<string, string> = {
    install_uv: `uv ${status.components?.uv?.identity?.replace(/^uv\s+/, "") || "runtime"}`,
    install_python: `CPython ${status.components?.managed_python?.identity?.match(/\d+\.\d+\.\d+/)?.[0] || "runtime"}`,
    install_model_manager: "Installing dependencies",
    install_backend_environment: "Installing dependencies",
    install_worker_environment: "Installing dependencies",
    install_models: "Preparing model",
  };
  const artifact = progress?.artifact_name || stageArtifact[stage]
    || (operation === "loading" ? "Loading model"
      : operation === "verifying" ? "Verifying installation"
        : operation === "installing" && kind === "Python package" ? "Installing dependencies" : "Preparing setup");
  const title = operation === "downloading" && kind === "Model" ? "Downloading models"
    : operation === "downloading" && kind === "Runtime" ? "Downloading runtime"
      : operation === "installing" ? artifact
        : operation === "verifying" ? "Verifying installation"
          : operation === "loading" ? "Loading model" : "Preparing Speechcraft";
  return { title, artifact, kind, progress };
}

function errorView(code: string, message = ""): ErrorView {
  const info = setupFailureFor(code, message);
  const title = info.category === "hugging_face_token" ? "Check your Hugging Face token"
    : info.category === "model_conditions" ? "Accept the model conditions"
      : info.category === "network" ? "Network unavailable"
        : info.category === "setup_controller" ? "Setup controller unavailable"
          : info.category === "backend" ? "Processing backend unavailable"
            : info.category === "python_runtime" ? "Python setup failed"
                : info.category === "model_initialization" ? "Model could not initialize"
              : info.category === "model_download" ? "Model download failed"
                : info.category === "model_verification" ? "Model verification failed"
                  : info.category === "interrupted" ? "Setup was interrupted"
                    : info.category === "prerequisites" ? "Processing requirements are missing"
                      : "Setup could not finish";
  return { category: title, message: info.message, action: info.action };
}

function errorFromException(error: unknown, managed: boolean): ErrorView {
  if (error instanceof SetupApiError) {
    if (error.status >= 500 && !error.code.startsWith("HF_")) {
      return errorView("SETUP_CONTROLLER_UNAVAILABLE");
    }
    return errorView(error.code, error.message);
  }
  if (error instanceof SpeechcraftApiError) {
    if (error.status === 0 || error.status >= 500) {
      return errorView(managed ? "BACKEND_UNAVAILABLE" : "NETWORK_UNAVAILABLE");
    }
    return errorView("MODEL_DOWNLOAD_FAILED", error.detail);
  }
  return errorView(managed ? "SETUP_CONTROLLER_UNAVAILABLE" : "NETWORK_UNAVAILABLE");
}

function ProgressScreen({ view }: { view: ProgressView }) {
  const progress = view.progress;
  const percent = downloadPercent(progress);
  const operation = progress?.operation || "checking";
  const downloading = operation === "downloading";
  const measuredBytes = downloading && progress?.progress_mode === "bytes";
  const currentBytes = measuredBytes && typeof progress?.transferred_bytes === "number" ? progress.transferred_bytes : null;
  const totalBytes = measuredBytes && typeof progress?.total_bytes === "number" ? progress.total_bytes : null;
  const downloaded = currentBytes !== null ? formatBytes(currentBytes) : null;
  const total = totalBytes !== null ? formatBytes(totalBytes) : null;
  const filesBased = downloading && progress?.progress_mode === "files"
    && Number.isSafeInteger(progress.completed_files) && Number.isSafeInteger(progress.total_files)
    && (progress.total_files || 0) > 0;
  const speed = measuredBytes && typeof progress?.bytes_per_second === "number" && progress.bytes_per_second > 0 && !progress.stalled
    ? `${formatBytes(progress.bytes_per_second)}/s` : null;
  const eta = measuredBytes && !progress?.stalled ? formatEta(progress?.eta_seconds) : null;
  const indeterminateLabel = operation === "installing" ? view.artifact
    : operation === "verifying" ? "Verifying installation"
      : operation === "loading" ? "Loading model"
        : operation === "checking" ? "Preparing Speechcraft" : view.artifact;
  return (
    <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">{view.title}</h1>
        <p className="font-sans text-sm text-[#878787]">
          {downloading ? `${view.artifact} is downloading.` : indeterminateLabel}
        </p>
      </div>
      <div className="mt-8 space-y-3" aria-live="polite">
        <div className="flex items-baseline justify-between gap-3 text-sm">
          <span className="truncate font-medium">{view.artifact}</span>
          <span className="shrink-0 text-xs text-[#878787]">{view.kind}</span>
        </div>
        {progress?.current_filename && downloading ? <p className="truncate text-xs text-[#878787]">{progress.current_filename}</p> : null}
        <div role="progressbar" aria-label={`${view.artifact} ${downloading ? "download" : operation}`}
          aria-valuemin={0} aria-valuemax={100} {...(percent === null ? {} : { "aria-valuenow": percent })}
          className="h-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
          <div className={`h-full bg-foreground/70 ${measuredBytes && percent !== null ? "transition-[width]" : "speechcraft-progress-indeterminate w-1/3"}`}
            style={measuredBytes && percent !== null ? { width: `${percent}%` } : undefined} />
        </div>
        {measuredBytes && downloaded && total ? <div className="flex justify-between gap-3 text-xs text-[#878787]">
          <span>{downloaded} / {total}</span>
          {percent === null ? null : <span>{percent}%</span>}
        </div> : null}
        {filesBased ? <p className="text-xs text-[#878787]">
          {progress?.completed_files} of {progress?.total_files} files
        </p> : null}
        {speed || eta ? <p className="text-xs text-[#878787]">
          {[speed ? `Speed ${speed}` : null, eta ? `ETA ${eta}` : null].filter(Boolean).join(" · ")}
        </p> : null}
      </div>
    </>
  );
}

export function ModelDownload() {
  const router = useRouter();
  const params = useSearchParams();
  const projectId = params.get("project");
  const demo = demoEnabled(params);
  const [phase, setPhase] = useState<Phase>("checking");
  const [managed, setManaged] = useState(false);
  const [token, setToken] = useState("");
  const [authorizationError, setAuthorizationError] = useState<string | null>(null);
  const [error, setError] = useState<ErrorView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState<ProgressView | null>(null);
  const [downloadModel, setDownloadModel] = useState<WhisperModelSize | null>(null);
  const startedRef = useRef(false);
  const busyRef = useRef(false);
  const finalizingRef = useRef(false);
  const watchingRef = useRef(false);
  const managedRefreshInFlightRef = useRef<Promise<SetupStatus | null> | null>(null);
  const managedRefreshPendingRef = useRef(false);
  const managedRevisionRef = useRef<{ jobId: string | null; attemptId: string | null; revision: number }>({
    jobId: null, attemptId: null, revision: -1,
  });
  const setupStopRef = useRef<(() => void) | null>(null);
  const standaloneTimerRef = useRef<number | null>(null);
  const monitorTimerRef = useRef<number | null>(null);

  const goToImport = useCallback(() => {
    const search = projectId ? `?project=${encodeURIComponent(projectId)}` : "";
    router.replace(withDemo(`/login2${search}`, demo));
  }, [router, projectId, demo]);

  const stopWatch = useCallback(() => {
    setupStopRef.current?.();
    setupStopRef.current = null;
    watchingRef.current = false;
    if (monitorTimerRef.current !== null) window.clearTimeout(monitorTimerRef.current);
    monitorTimerRef.current = null;
  }, []);

  const showFailure = useCallback((value: ErrorView) => {
    stopWatch();
    setError(value);
    if (value.action === "verify") {
      setAuthorizationError(value.message);
      setPhase("needs_authorization");
    } else setPhase("error");
  }, [stopWatch]);

  const verifyFinalBackend = useCallback(async () => {
    if (finalizingRef.current) return;
    finalizingRef.current = true;
    try {
      const result: AsrAvailability = await resolveWhisperModel();
      if (result.status === "ready") { goToImport(); return; }
      const detail = result.status === "error" ? result.error : "Whisper is not verified in the managed runtime.";
      const code = result.status === "error" && result.category === "model_unusable"
        ? "MODEL_INITIALIZATION_FAILED" : "PROCESSING_PREREQUISITES_MISSING";
      showFailure(errorView(code, detail));
    } catch (failure) {
      showFailure(errorFromException(failure, true));
    } finally {
      finalizingRef.current = false;
    }
  }, [goToImport, showFailure]);

  const processManagedStatus = useCallback(async (status: SetupStatus, models: ModelState[]) => {
    const action = setupActionFor(status, models);
    if (action === "watch") {
      setError(null);
      setProgress(setupProgress(status, models));
      setPhase("downloading");
      return;
    }
    stopWatch();
    if (action === "finalize") {
      await verifyFinalBackend();
      return;
    }
    if (action === "verify") {
      const authMessage = status.job.error?.code ? setupFailureFor(status.job.error.code, status.job.error.message).message : null;
      setAuthorizationError(authMessage);
      setPhase("needs_authorization");
      return;
    }
    if (action === "download") {
      setError(null);
      setPhase("access_verified");
      return;
    }
    const code = setupFailureCodeFor(status);
    showFailure(errorView(code, status.job.error?.message || ""));
  }, [stopWatch, verifyFinalBackend, showFailure]);

  const applyManagedSnapshot = useCallback(async (status: SetupStatus, list: Awaited<ReturnType<typeof setupApi.models>>) => {
    // Status and model rows are fetched separately. If model rows came from a
    // later revision, discard this mismatched pair and fetch a coherent view.
    if (list.state_revision > status.state_revision) {
      managedRefreshPendingRef.current = true;
      return status;
    }
    const revision = status.state_revision;
    const previous = managedRevisionRef.current;
    const sameAttempt = previous.jobId === status.job.id && previous.attemptId === (status.job.attempt_id ?? null);
    if (sameAttempt && revision <= previous.revision) return status;
    managedRevisionRef.current = { jobId: status.job.id, attemptId: status.job.attempt_id ?? null, revision };
    const models = list.state_revision === status.state_revision ? list.models : [];
    await processManagedStatus(status, models);
    return status;
  }, [processManagedStatus]);

  const refreshManagedJob = useCallback(() => {
    if (managedRefreshInFlightRef.current) {
      managedRefreshPendingRef.current = true;
      return managedRefreshInFlightRef.current;
    }
    const loadLatest = async () => {
      let latest: SetupStatus | null = null;
      do {
        managedRefreshPendingRef.current = false;
        const [status, list] = await Promise.all([setupApi.status(), setupApi.models()]);
        latest = await applyManagedSnapshot(status, list);
      } while (managedRefreshPendingRef.current);
      return latest;
    };
    const inFlight = loadLatest().finally(() => {
      if (managedRefreshInFlightRef.current === inFlight) managedRefreshInFlightRef.current = null;
    });
    managedRefreshInFlightRef.current = inFlight;
    return inFlight;
  }, [applyManagedSnapshot]);

  const inspectManagedSetup = useCallback(async () => {
    await setupApi.ensureSession();
    return refreshManagedJob();
  }, [refreshManagedJob]);

  const watchManagedJob = useCallback(() => {
    if (watchingRef.current) return;
    watchingRef.current = true;
    let disconnectedAt: number | null = null;
    const timeout = () => {
      if (!watchingRef.current || disconnectedAt === null) return;
      if (Date.now() - disconnectedAt >= 45_000) {
        showFailure(errorView("SETUP_CONTROLLER_UNAVAILABLE"));
        return;
      }
      monitorTimerRef.current = window.setTimeout(timeout, 5_000);
    };
    setupStopRef.current = subscribeSetupEvents(
      () => {
        void refreshManagedJob().catch((failure) => showFailure(errorFromException(failure, true)));
      },
      () => {
        disconnectedAt = null;
        if (monitorTimerRef.current !== null) window.clearTimeout(monitorTimerRef.current);
        monitorTimerRef.current = null;
      },
      () => {
        if (disconnectedAt === null) disconnectedAt = Date.now();
        if (monitorTimerRef.current === null) monitorTimerRef.current = window.setTimeout(timeout, 5_000);
      },
    );
    void refreshManagedJob().catch((failure) => showFailure(errorFromException(failure, true)));
  }, [refreshManagedJob, showFailure]);

  const monitorStandaloneJob = useCallback((jobId: string, model: WhisperModelSize) => {
    if (standaloneTimerRef.current !== null) window.clearInterval(standaloneTimerRef.current);
    let consecutiveErrors = 0;
    let inFlight = false;
    let latestEventRevision = -1;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const job = await fetchAsrEnsure(jobId);
        if (job.id !== jobId || (job.progress.job_id && job.progress.job_id !== jobId)
            || (Number.isSafeInteger(job.progress.event_revision) && job.progress.event_revision! <= latestEventRevision)) return;
        if (Number.isSafeInteger(job.progress.event_revision)) latestEventRevision = job.progress.event_revision!;
        consecutiveErrors = 0;
        const downloading = job.phase === "queued" || job.phase === "downloading";
        setPhase("downloading");
        setProgress({ title: downloading ? "Downloading models" : job.progress.operation === "verifying" ? "Verifying installation" : "Preparing Speechcraft",
          artifact: job.progress.artifact_name || "Preparing model", kind: "Model", progress: job.progress });
        if (job.phase === "complete" && job.ok) {
          if (standaloneTimerRef.current !== null) window.clearInterval(standaloneTimerRef.current);
          standaloneTimerRef.current = null;
          storeEnsureJobId(model, null);
          await finalizeStandalone();
        } else if (job.phase === "failed" || job.ok === false) {
          if (standaloneTimerRef.current !== null) window.clearInterval(standaloneTimerRef.current);
          standaloneTimerRef.current = null;
          showFailure(errorView("MODEL_DOWNLOAD_FAILED", job.error || "The Whisper model download failed."));
        }
      } catch (failure) {
        consecutiveErrors += 1;
        if (consecutiveErrors >= 3) {
          if (standaloneTimerRef.current !== null) window.clearInterval(standaloneTimerRef.current);
          standaloneTimerRef.current = null;
          storeEnsureJobId(model, null);
          showFailure(errorView("MODEL_DOWNLOAD_FAILED", "The previous download job is no longer available. Retry to start a new download."));
        }
      } finally {
        inFlight = false;
      }
    };
    void poll();
    standaloneTimerRef.current = window.setInterval(() => { void poll(); }, 1_500);
  }, [showFailure]);

  const finalizeStandalone = useCallback(async () => {
    try {
      const result = await resolveWhisperModel();
      if (result.status === "ready") goToImport();
      else showFailure(errorView(result.status === "error" && result.category === "model_unusable"
        ? "MODEL_INITIALIZATION_FAILED" : "PROCESSING_PREREQUISITES_MISSING",
      result.status === "error" ? result.error : "Whisper is not ready after download."));
    } catch (failure) { showFailure(errorFromException(failure, false)); }
  }, [goToImport, showFailure]);

  const startStandaloneDownload = useCallback(async () => {
    if (!downloadModel || busyRef.current) return;
    busyRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      let job: AsrEnsureJob | null = null;
      const savedId = readEnsureJobId(downloadModel);
      if (savedId) {
        try { job = await fetchAsrEnsure(savedId); }
        catch (failure) {
          if (failure instanceof SpeechcraftApiError && failure.status === 404) {
            storeEnsureJobId(downloadModel, null);
          } else throw failure;
        }
      }
      if (!job || ["failed", "complete"].includes(job.phase)) {
        if (job?.phase === "complete" && job.ok) { await finalizeStandalone(); return; }
        job = await startAsrEnsure(downloadModel);
      }
      storeEnsureJobId(downloadModel, job.id);
      monitorStandaloneJob(job.id, downloadModel);
    } catch (failure) {
      const detail = failure instanceof SpeechcraftApiError ? failure.detail : "Could not start the model download.";
      if (/managed_runtime_use_setup/i.test(detail)) {
        showFailure(errorView("SETUP_CONTROLLER_UNAVAILABLE", "This installation requires the managed setup controller. Restart Speechcraft with its launcher, then retry."));
      } else showFailure(errorFromException(failure, false));
    } finally {
      busyRef.current = false;
      setSubmitting(false);
    }
  }, [downloadModel, finalizeStandalone, monitorStandaloneJob, showFailure]);

  const initialize = useCallback(async () => {
    if (demo) { goToImport(); return; }
    setPhase("checking");
    setError(null);
    let launcherManaged = false;
    try {
      launcherManaged = await isLauncherManagedInstallation();
      if (!launcherManaged) {
        try {
          await setupApi.health();
          await setupApi.ensureSession();
          launcherManaged = (await setupApi.status()).mode === "runtime";
        } catch { /* an unmanaged development server may not run the setup controller */ }
      }
      setManaged(launcherManaged);
      if (launcherManaged) {
        const status = await inspectManagedSetup();
        if (status?.job.state === "running") watchManagedJob();
        return;
      }
      const result = await resolveWhisperModel();
      if (result.status === "ready") { goToImport(); return; }
      if (result.status === "needs_download") {
        setDownloadModel(result.downloadModel);
        setPhase("needs_download");
        const savedId = readEnsureJobId(result.downloadModel);
        if (savedId) {
          try {
            const saved = await fetchAsrEnsure(savedId);
            if (!["failed", "complete"].includes(saved.phase)) {
              storeEnsureJobId(result.downloadModel, saved.id);
              monitorStandaloneJob(saved.id, result.downloadModel);
            } else if (saved.phase === "complete" && saved.ok) await finalizeStandalone();
            else storeEnsureJobId(result.downloadModel, null);
          } catch (failure) {
            if (failure instanceof SpeechcraftApiError && failure.status === 404) storeEnsureJobId(result.downloadModel, null);
            else throw failure;
          }
        }
        return;
      }
      showFailure(errorView(result.category === "model_unusable" ? "MODEL_INITIALIZATION_FAILED" : "PROCESSING_PREREQUISITES_MISSING", result.error));
    } catch (failure) {
      showFailure(errorFromException(failure, launcherManaged));
    }
  }, [demo, goToImport, inspectManagedSetup, watchManagedJob, monitorStandaloneJob, finalizeStandalone, showFailure]);

  const verifyToken = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = token.trim();
    if (!value || busyRef.current) return;
    busyRef.current = true;
    setSubmitting(true);
    setAuthorizationError(null);
    try {
      const result = await setupApi.preauthorizePyannote(value);
      setToken("");
      if (!result.verified) throw new SetupApiError(502, "HF_VERIFICATION_FAILED", "Hugging Face access could not be verified.");
      setPhase("access_verified");
    } catch (failure) {
      const view = errorFromException(failure, true);
      setAuthorizationError(view.message);
    } finally {
      busyRef.current = false;
      setSubmitting(false);
    }
  }, [token]);

  const downloadManagedModels = useCallback(async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const response = await setupApi.repair();
      const job = (response as { job?: { state?: string } }).job;
      const status = await setupApi.status();
      if (status.job.state === "waiting_for_user") {
        setAuthorizationError("Your verification expired or the controller restarted. Verify your Hugging Face access again.");
        setPhase("needs_authorization");
      } else if (job?.state === "running" || status.job.state === "running") {
        setPhase("downloading");
        setProgress({ title: "Downloading models", artifact: status.job.progress?.message || "Speechcraft runtime", kind: "Runtime", progress: status.job.progress || null });
        watchManagedJob();
      } else {
        await refreshManagedJob();
      }
    } catch (failure) {
      showFailure(errorFromException(failure, true));
    } finally {
      busyRef.current = false;
      setSubmitting(false);
    }
  }, [refreshManagedJob, showFailure, watchManagedJob]);

  const retry = useCallback(async () => {
    if (!managed || error?.category === "Setup controller unavailable") {
      await initialize();
      return;
    }
    await downloadManagedModels();
  }, [managed, error, initialize, downloadManagedModels]);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    void initialize();
    return () => {
      stopWatch();
      if (standaloneTimerRef.current !== null) window.clearInterval(standaloneTimerRef.current);
      standaloneTimerRef.current = null;
    };
  }, [initialize, stopWatch]);

  if (phase === "error" && error) {
    return <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">{error.category}</h1>
        <p className="font-sans text-sm text-[#878787]">{error.message}</p>
      </div>
      <div className="mt-10 lg:mt-12">
        <SubmitButton type="button" onClick={() => void retry()}
          className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full" isSubmitting={submitting}>
          <span>Retry</span>
        </SubmitButton>
      </div>
    </>;
  }

  if (phase === "needs_authorization") {
    return <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Connect Hugging Face</h1>
        <p className="font-sans text-sm text-[#878787]">Speechcraft needs permission to download the speaker detection model.</p>
      </div>
      <ol className="mt-5 list-inside list-decimal space-y-2 text-center text-sm text-[#878787]">
        <li><a className="underline underline-offset-2" href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noreferrer">Accept the pyannote Community-1 model conditions</a></li>
        <li><a className="underline underline-offset-2" href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer">Create a Hugging Face read-access token</a></li>
      </ol>
      <form className="mt-8 flex flex-col gap-3" onSubmit={verifyToken}>
        <label className="sr-only" htmlFor="speechcraft-hf-token">Hugging Face token</label>
        <input id="speechcraft-hf-token" type="password" autoComplete="off" spellCheck={false} value={token}
          onChange={(event) => setToken(event.target.value)} placeholder="hf_…"
          className="h-10 w-full rounded-md border border-white/15 bg-transparent px-3 text-sm outline-none focus:border-white/40" />
        {authorizationError ? <p role="alert" className="text-sm text-[#c98b8b]">{authorizationError}</p> : null}
        <SubmitButton type="submit" disabled={!token.trim()} className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full" isSubmitting={submitting}>
          <span>Verify</span>
        </SubmitButton>
      </form>
    </>;
  }

  if (phase === "access_verified") {
    return <>
      <div className="text-center space-y-2">
        <div aria-hidden="true" className="mx-auto mb-2 flex h-7 w-7 items-center justify-center rounded-full border border-white/30 text-sm">✓</div>
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Access verified</h1>
        <p className="font-sans text-sm text-[#878787]">You&apos;re ready to download the models Speechcraft needs.</p>
      </div>
      <div className="mt-10 lg:mt-12">
        <SubmitButton type="button" onClick={() => managed ? void downloadManagedModels() : void startStandaloneDownload()}
          className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full" isSubmitting={submitting}>
          <span>Download Models</span>
        </SubmitButton>
      </div>
    </>;
  }

  if (phase === "needs_download") {
    return <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Download Models</h1>
        <p className="font-sans text-sm text-[#878787]">Speechcraft needs Whisper {downloadModel || "large-v3"} before importing recordings.</p>
      </div>
      <div className="mt-10 lg:mt-12">
        <SubmitButton type="button" onClick={() => void startStandaloneDownload()} className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full" isSubmitting={submitting}>
          <span>Download Models</span>
        </SubmitButton>
      </div>
    </>;
  }

  if (phase === "downloading" && progress) return <ProgressScreen view={progress} />;

  return <>
    <div className="text-center space-y-2">
      <h1 className="text-lg lg:text-xl mb-4 font-serif">Checking Speechcraft</h1>
      <p className="font-sans text-sm text-[#878787]">Checking the installed runtime and required models…</p>
    </div>
    <div className="mt-10 lg:mt-12 flex items-center justify-center"><Spinner size={20} /></div>
  </>;
}
