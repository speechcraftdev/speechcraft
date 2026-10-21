"use client";

import { Icons } from "@midday/ui/icons";
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
  createDatasetRun,
  fetchAsrEnsure,
  findReusableWizardRun,
  resolveWhisperModel,
  startAsrEnsure,
  startRun,
} from "./wizard/wizard-api";
import { demoEnabled, withDemo } from "@/lib/demo";
import {
  type ModelState,
  type ProgressTelemetry,
  SetupApiError,
  setupApi,
  subscribeSetupEvents,
} from "@/lib/setup-api";

type Phase =
  | "checking"
  | "needs_download"
  | "needs_authorization"
  | "downloading"
  | "loading"
  | "starting"
  | "error";

type ProgressView = {
  title: string;
  subtitle: string;
  packageType?: string | null;
  progress: ProgressTelemetry | AsrEnsureProgress | null;
};

function runStorageKey(projectId: string) {
  return `speechcraft-login3-run:${projectId}`;
}

function attemptStorageKey(projectId: string) {
  return `speechcraft-login3-attempt:${projectId}`;
}

function getWizardAttemptId(projectId: string) {
  const key = attemptStorageKey(projectId);
  try {
    const current = localStorage.getItem(key);
    if (current) return current;
    const created = `login3:${projectId}:${crypto.randomUUID()}`;
    localStorage.setItem(key, created);
    return created;
  } catch {
    try {
      const current = sessionStorage.getItem(key);
      if (current) return current;
      const created = `login3:${projectId}:${crypto.randomUUID()}`;
      sessionStorage.setItem(key, created);
      return created;
    } catch {
      return `login3:${projectId}`;
    }
  }
}

function ensureStorageKey(projectId: string | null, model: WhisperModelSize) {
  return `speechcraft-login3-ensure:${projectId || "pre-import"}:${model}`;
}

function readEnsureJobId(projectId: string | null, model: WhisperModelSize): string | null {
  try { return localStorage.getItem(ensureStorageKey(projectId, model)); } catch { return null; }
}

function storeEnsureJobId(projectId: string | null, model: WhisperModelSize, jobId: string | null) {
  try {
    if (jobId) localStorage.setItem(ensureStorageKey(projectId, model), jobId);
    else localStorage.removeItem(ensureStorageKey(projectId, model));
  } catch { /* storage may be unavailable */ }
}

function storeRunId(projectId: string, runId: string) {
  try {
    localStorage.setItem(runStorageKey(projectId), runId);
  } catch {
    /* private browsing */
  }
}

function formatBytes(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value < 1_000) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = -1;
  do {
    amount /= 1_000;
    unit += 1;
  } while (amount >= 1_000 && unit < units.length - 1);
  return `${amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)} ${units[unit]}`;
}

function formatDuration(seconds: number | null | undefined) {
  if (!Number.isSafeInteger(seconds) || seconds! < 0) return null;
  if (seconds === 0) return "Less than a second";
  if (seconds! < 60) return `About ${seconds} sec left`;
  const minutes = Math.ceil(seconds! / 60);
  if (minutes < 60) return `About ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `About ${hours} hr ${remainder} min left` : `About ${hours} hr left`;
}

function RestrainedProgress({ view }: { view: ProgressView }) {
  const progress = view.progress;
  const phase = progress?.phase || null;
  const isDownload = phase === "downloading";
  const downloadedBytes = progress?.downloaded_bytes ?? null;
  const totalBytes = progress?.total_bytes ?? null;
  const downloaded = formatBytes(downloadedBytes);
  const total = formatBytes(totalBytes);
  const calculatedPercent =
    typeof downloadedBytes === "number" && typeof totalBytes === "number" && totalBytes > 0
      ? Math.floor((downloadedBytes * 100) / totalBytes)
      : null;
  const reportedPercent =
    progress && "percent" in progress && typeof progress.percent === "number"
      ? progress.percent
      : null;
  const percent =
    isDownload &&
    downloaded &&
    total &&
    (calculatedPercent !== null || reportedPercent !== null) &&
    !progress?.transfer_indeterminate &&
    !progress?.indeterminate
      ? Math.max(0, Math.min(100, calculatedPercent ?? reportedPercent ?? 0))
      : null;
  const speed =
    isDownload &&
    !progress?.transfer_indeterminate &&
    !progress?.indeterminate &&
    !progress?.stalled &&
    typeof progress?.bytes_per_second === "number" &&
    progress.bytes_per_second > 0
      ? `${formatBytes(progress.bytes_per_second)}/s`
      : null;
  const eta =
    isDownload && !progress?.transfer_indeterminate && !progress?.indeterminate && !progress?.stalled
      ? formatDuration(progress?.eta_seconds)
      : null;
  const label = progress?.model_name || view.title;
  const packageType =
    (progress && "package_type" in progress ? progress.package_type : null) || view.packageType;
  const fileCount =
    isDownload &&
    Number.isSafeInteger(progress?.files_completed) &&
    Number.isSafeInteger(progress?.files_total)
      ? `${progress?.files_completed} / ${progress?.files_total} files`
      : null;

  return (
    <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">{view.title}</h1>
        <p className="font-sans text-sm text-[#878787]">{view.subtitle}</p>
      </div>
      <div className="mt-8 space-y-3" aria-live="polite">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
          <span className="font-medium truncate">{label}</span>
          {packageType ? (
            <span className="text-xs text-[#878787] shrink-0">{packageType}</span>
          ) : null}
        </div>
        {progress?.current_filename && isDownload ? (
          <p className="truncate text-xs text-[#878787]">{progress.current_filename}</p>
        ) : null}
        <div
          role="progressbar"
          aria-label={`${label}: ${isDownload ? "downloading" : "preparing"}`}
          aria-valuemin={0}
          aria-valuemax={100}
          {...(percent === null ? {} : { "aria-valuenow": percent })}
          className="h-1 overflow-hidden rounded-full bg-black/10 dark:bg-white/10"
        >
          <div
            className={`h-full bg-foreground/70 ${percent === null ? "w-1/3 animate-pulse" : "transition-[width]"}`}
            style={percent === null ? undefined : { width: `${percent}%` }}
          />
        </div>
        {isDownload && downloaded && total ? (
          <div className="flex items-baseline justify-between gap-3 text-xs text-[#878787]">
            <span>
              {downloaded} / {total}
            </span>
            {percent !== null ? <span>{percent}%</span> : null}
          </div>
        ) : null}
        {isDownload && downloaded && !total ? (
          <p className="text-xs text-[#878787]">Downloaded: {downloaded}</p>
        ) : null}
        {fileCount ? <p className="text-xs text-[#878787]">{fileCount}</p> : null}
        {isDownload ? (
          <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 text-xs text-[#878787]">
            <span>Speed: {progress?.stalled ? "Stalled" : speed || "unavailable"}</span>
            <span>
              ETA:{" "}
              {progress?.stalled
                ? "Unavailable"
                : eta ||
                  (progress?.transfer_indeterminate || progress?.indeterminate
                    ? "unavailable"
                    : "Calculating…")}
            </span>
          </div>
        ) : (
          <p className="text-xs text-[#878787]">
            {phase === "loading" || phase === "verifying" || phase === "installing"
              ? "Loading and verifying the model on this machine…"
              : "Working…"}
          </p>
        )}
      </div>
    </>
  );
}

function pickActiveSetupModel(models: ModelState[]): ModelState | null {
  const asr = models.find((m) => m.role === "asr");
  if (!asr) return null;
  if (asr.progress?.phase === "downloading" || asr.state === "downloading") return asr;
  const active = models.find(
    (m) =>
      m.progress?.phase === "downloading" ||
      ["downloading", "installing", "verifying_files", "validating_inference"].includes(m.state),
  );
  return active || asr;
}

export function ModelDownload() {
  const router = useRouter();
  const params = useSearchParams();
  const projectId = params.get("project");
  const demo = demoEnabled(params);
  const [phase, setPhase] = useState<Phase>("checking");
  const [model, setModel] = useState<WhisperModelSize | null>(null);
  const [downloadTarget, setDownloadTarget] = useState<{
    model: WhisperModelSize;
    repoId: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [progressView, setProgressView] = useState<ProgressView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [hfToken, setHfToken] = useState("");
  const [authorizationError, setAuthorizationError] = useState<string | null>(null);
  const startedRef = useRef(false);
  const busyRef = useRef(false);
  const setupStopRef = useRef<(() => void) | null>(null);

  const navigateToLogin4 = useCallback(
    (runId: string) => {
      if (projectId) storeRunId(projectId, runId);
      router.push(
        withDemo(
          `/login4?project=${encodeURIComponent(projectId ?? "demo-review")}&run=${encodeURIComponent(runId)}`,
          demo,
        ),
      );
    },
    [router, projectId, demo],
  );

  const skipAhead = useCallback(() => {
    const runId = params.get("run") ?? "demo";
    router.push(
      withDemo(
        `/login4?project=${encodeURIComponent(projectId ?? "demo-review")}&run=${encodeURIComponent(runId)}`,
        true,
      ),
    );
  }, [router, projectId, params]);

  const ensureDatasetRun = useCallback(
    async (whisperModel: WhisperModelSize) => {
      if (!projectId) throw new SpeechcraftApiError(0, "No project in the URL. Restart from ingest.");

      const attemptId = getWizardAttemptId(projectId);
      const reusable = await findReusableWizardRun(projectId, attemptId, whisperModel);
      if (reusable) {
        storeRunId(projectId, reusable.id);
        if (reusable.status === "pending") {
          await startRun(reusable.id);
        }
        return reusable.id;
      }

      const datasetRun = await createDatasetRun(projectId, {
        whisper_model_size: whisperModel,
        wizard_attempt: attemptId,
      });
      storeRunId(projectId, datasetRun.id);
      await startRun(datasetRun.id);
      return datasetRun.id;
    },
    [projectId],
  );

  const finishWithModel = useCallback(
    async (whisperModel: WhisperModelSize) => {
      setModel(whisperModel);
      setPhase("starting");
      setProgressView({
        title: "Starting speaker detection",
        subtitle: `Using ${whisperModel} · preparing the run…`,
        packageType: "faster-whisper",
        progress: { phase: "installing", model_name: whisperModel, indeterminate: true },
      });
      const runId = await ensureDatasetRun(whisperModel);
      navigateToLogin4(runId);
    },
    [ensureDatasetRun, navigateToLogin4],
  );

  const continueWithModel = useCallback(async (whisperModel: WhisperModelSize) => {
    if (projectId) {
      await finishWithModel(whisperModel);
      return;
    }
    router.replace(withDemo("/login2", demo));
  }, [projectId, finishWithModel, router, demo]);

  const stopSetupWatch = useCallback(() => {
    setupStopRef.current?.();
    setupStopRef.current = null;
  }, []);

  const waitForSetupModels = useCallback(async () => {
    setPhase("downloading");
    setProgressView({
      title: "Preparing SpeechCraft",
      subtitle: "Checking required processing components…",
      packageType: "setup",
      progress: { phase: "checking", model_name: "SpeechCraft runtime", indeterminate: true },
    });

    const refreshModels = async () => {
      const list = await setupApi.models();
      const active = pickActiveSetupModel(list.models);
      if (active) {
        const downloading =
          active.progress?.phase === "downloading" || active.state === "downloading";
        setPhase(downloading ? "downloading" : "loading");
        setProgressView({
          title: downloading ? "Downloading models" : "Loading models",
          subtitle: downloading
            ? "Transferring model files…"
            : "Verifying and initializing installed models…",
          packageType: active.role === "asr" ? "whisper" : active.role,
          progress: active.progress
            ? { ...active.progress, model_name: active.progress.model_name || active.display_name }
            : {
                phase: downloading ? "downloading" : "installing",
                model_name: active.display_name,
                indeterminate: true,
              },
        });
      }
      const status = await setupApi.status();
      const needsAction =
        status.job.state === "waiting_for_user" ||
        list.models.some((m) => m.action_required || m.state === "waiting_for_token");
      if (needsAction) {
        const pyannoteNeedsAuthorization = list.models.some(
          (m) => m.role === "diarization" && (m.action_required || m.state === "waiting_for_token"),
        );
        if (!pyannoteNeedsAuthorization) {
          throw new SpeechcraftApiError(0, "A required model needs authorization before setup can continue.");
        }
        setAuthorizationError(null);
        setPhase("needs_authorization");
        return false;
      }
      if (status.job.state === "failed") {
        throw new SpeechcraftApiError(
          0,
          status.job.error?.message || "Model setup failed. Retry after the runtime is repaired.",
        );
      }
      return (
        status.technical_processing_ready ||
        list.models.some((m) => m.role === "asr" && (m.runtime_ready || m.files_verified))
      );
    };

    try {
      await setupApi.ensureSession();
      const status = await setupApi.status();
      if (status.job.state === "failed" || status.job.state === "cancelled" || status.job.state === "idle") {
        try {
          await setupApi.repair();
        } catch {
          await setupApi.start();
        }
      } else if (status.job.state === "completed" && !status.technical_processing_ready) {
        await setupApi.repair();
      }
    } catch (err) {
      throw err instanceof SetupApiError
        ? new SpeechcraftApiError(err.status, err.message)
        : err;
    }

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        stopSetupWatch();
        if (error) reject(error);
        else resolve();
      };
      const stop = subscribeSetupEvents(
        async () => {
          try {
            if (await refreshModels()) finish();
          } catch (err) {
            finish(err instanceof Error ? err : new Error("Setup progress failed"));
          }
        },
        () => {
          void refreshModels()
            .then((ready) => {
              if (ready) finish();
            })
            .catch((err) => finish(err instanceof Error ? err : new Error("Setup reconnect failed")));
        },
        () => {
          /* transient SSE errors retry inside the helper */
        },
      );
      setupStopRef.current = stop;
      void refreshModels()
        .then((ready) => {
          if (ready) finish();
        })
        .catch((err) => finish(err instanceof Error ? err : new Error("Setup status failed")));
      window.setTimeout(() => {
        finish(new Error("Timed out waiting for model setup."));
      }, 3_600_000);
    });
  }, [stopSetupWatch]);

  const submitPyannoteAuthorization = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const token = hfToken.trim();
    if (!token || submitting) return;
    setSubmitting(true);
    setAuthorizationError(null);
    try {
      await setupApi.authorizePyannote(token);
      setHfToken("");
      setPhase("downloading");
    } catch (err) {
      setAuthorizationError(err instanceof SetupApiError ? err.message : "Could not verify the Hugging Face token.");
    } finally {
      setSubmitting(false);
    }
  }, [hfToken, submitting]);

  const waitForBackendEnsure = useCallback(async (target: WhisperModelSize) => {
    setPhase("downloading");
    setProgressView({
      title: "Downloading models",
      subtitle: "Downloading the Whisper package…",
      packageType: "faster-whisper",
      progress: {
        phase: "downloading",
        model_name: target,
        package_type: "faster-whisper",
        transfer_indeterminate: true,
        indeterminate: true,
      },
    });

    let job: AsrEnsureJob;
    const savedJobId = readEnsureJobId(projectId, target);
    if (savedJobId) {
      try { job = await fetchAsrEnsure(savedJobId); }
      catch { job = await startAsrEnsure(target); }
    } else {
      try { job = await startAsrEnsure(target); }
      catch (err) {
        if (err instanceof SpeechcraftApiError && err.status === 409) {
          await waitForSetupModels();
          return;
        }
        throw err;
      }
    }
    storeEnsureJobId(projectId, target, job.id);

    for (;;) {
      const latest = await fetchAsrEnsure(job.id);
      const downloading = latest.phase === "downloading" || latest.phase === "queued";
      setPhase(downloading ? "downloading" : latest.phase === "complete" ? "loading" : "downloading");
      setProgressView({
        title: downloading ? "Downloading models" : "Preparing models",
        subtitle: downloading
          ? "Downloading the Whisper package…"
          : "Verifying the downloaded model…",
        packageType: latest.package_type || "faster-whisper",
        progress: {
          ...latest.progress,
          phase: latest.progress.phase || latest.phase,
          model_name: latest.progress.model_name || latest.model,
          package_type: latest.package_type,
        },
      });
      if (latest.phase === "complete" && latest.ok) {
        storeEnsureJobId(projectId, target, null);
        return;
      }
      if (latest.phase === "failed" || latest.ok === false) {
        if (latest.reason_codes?.includes("managed_runtime_use_setup")) {
          await waitForSetupModels();
          return;
        }
        throw new SpeechcraftApiError(0, latest.error || "Model download failed.");
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }, [waitForSetupModels, projectId]);

  const checkAndMaybePrompt = useCallback(async () => {
    if (demo) {
      skipAhead();
      return;
    }
    setPhase("checking");
    setError(null);
    try {
      // URL and stored runs use the same project, attempt, configuration and input checks.
      const urlRun = params.get("run");
      if (projectId && urlRun && urlRun !== "demo") {
        const attemptId = getWizardAttemptId(projectId);
        const candidate = await findReusableWizardRun(projectId, attemptId, undefined, urlRun);
        if (candidate) {
          if (candidate.status === "pending") {
            setPhase("starting");
            await startRun(candidate.id);
          }
          navigateToLogin4(candidate.id);
          return;
        }
      }
      let resolved: AsrAvailability;
      try {
        resolved = await resolveWhisperModel();
      } catch (err) {
        if (err instanceof SpeechcraftApiError && err.status > 0 && err.status < 500) throw err;
        await waitForSetupModels();
        resolved = await resolveWhisperModel();
      }
      if (resolved.status === "ready") {
        if (!projectId) {
          await continueWithModel(resolved.model);
          return;
        }
        const attemptId = getWizardAttemptId(projectId);
        const reusable = await findReusableWizardRun(projectId, attemptId, resolved.model);
        if (reusable) {
          if (reusable.status === "pending") await startRun(reusable.id);
          navigateToLogin4(reusable.id);
          return;
        }
        await continueWithModel(resolved.model);
        return;
      }
      if (resolved.status === "needs_download") {
        const activeJobId = readEnsureJobId(projectId, resolved.downloadModel);
        if (activeJobId) {
          try {
            const activeJob = await fetchAsrEnsure(activeJobId);
            if (["queued", "downloading", "verifying", "complete"].includes(activeJob.phase)) {
              if (activeJob.phase === "complete" && activeJob.ok) {
                const after = await resolveWhisperModel();
                if (after.status === "ready") { await continueWithModel(after.model); return; }
              } else {
                setDownloadTarget({ model: resolved.downloadModel, repoId: resolved.repoId });
                setModel(resolved.downloadModel);
                busyRef.current = true;
                setPhase("downloading");
                void waitForBackendEnsure(resolved.downloadModel).then(async () => {
                  const after = await resolveWhisperModel();
                  if (after.status !== "ready") throw new Error(after.error || "The downloaded model is not ready.");
                  await continueWithModel(after.model);
                }).catch((err) => {
                  setPhase("error");
                  setError(err instanceof SpeechcraftApiError ? err.detail : "Could not resume the model download.");
                }).finally(() => { busyRef.current = false; });
                return;
              }
            } else {
              storeEnsureJobId(projectId, resolved.downloadModel, null);
            }
          } catch { storeEnsureJobId(projectId, resolved.downloadModel, null); }
        }
        setDownloadTarget({ model: resolved.downloadModel, repoId: resolved.repoId });
        setModel(resolved.downloadModel);
        setPhase("needs_download");
        setProgressView(null);
        return;
      }
      setPhase("error");
      setError(resolved.error);
    } catch (err) {
      if (downloadTarget) storeEnsureJobId(projectId, downloadTarget.model, null);
      setPhase("error");
      setError(err instanceof SpeechcraftApiError ? err.detail : "Could not prepare the model.");
    }
  }, [demo, projectId, params, skipAhead, navigateToLogin4, continueWithModel, waitForBackendEnsure, waitForSetupModels]);

  const beginDownload = useCallback(async () => {
    if (busyRef.current || !downloadTarget) return;
    busyRef.current = true;
    setSubmitting(true);
    setError(null);
    try {
      await waitForBackendEnsure(downloadTarget.model);
      setPhase("loading");
      setProgressView({
        title: "Loading models",
        subtitle: "Checking that the model can run on this hardware…",
        packageType: "faster-whisper",
        progress: {
          phase: "loading",
          model_name: downloadTarget.model,
          package_type: "faster-whisper",
          indeterminate: true,
        },
      });
      const resolved = await resolveWhisperModel();
      if (resolved.status !== "ready") {
        setPhase("error");
        setError(
          resolved.status === "error"
            ? resolved.error
            : "The model downloaded, but is still not ready. Retry after required runtime components are available.",
        );
        return;
      }
      await continueWithModel(resolved.model);
    } catch (err) {
      setPhase("error");
      setError(err instanceof SpeechcraftApiError ? err.detail : "Could not download the model.");
    } finally {
      busyRef.current = false;
      setSubmitting(false);
      stopSetupWatch();
    }
  }, [downloadTarget, waitForBackendEnsure, continueWithModel, stopSetupWatch]);

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    void checkAndMaybePrompt();
    return () => {
      stopSetupWatch();
    };
  }, [checkAndMaybePrompt, stopSetupWatch]);

  if (phase === "error") {
    return (
      <>
        <div className="text-center space-y-2">
          <h1 className="text-lg lg:text-xl mb-4 font-serif">Couldn&apos;t prepare a model</h1>
          <p className="font-sans text-sm text-[#878787]">{error}</p>
        </div>
        <div className="mt-10 lg:mt-12">
          <SubmitButton
            type="button"
            onClick={() => {
              startedRef.current = false;
              busyRef.current = false;
              void checkAndMaybePrompt();
            }}
            className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
            isSubmitting={false}
          >
            <div className="flex items-center justify-center gap-2">
              <Icons.HuggingFace size={16} />
              <span>Retry</span>
            </div>
          </SubmitButton>
        </div>
        <button
          type="button"
          onClick={skipAhead}
          className="mt-4 w-full text-center text-xs text-[#878787] underline hover:text-foreground"
        >
          Skip ahead with demo data (no model needed)
        </button>
      </>
    );
  }

  if (phase === "needs_download" && downloadTarget) {
    return (
      <>
        <div className="text-center space-y-2">
          <h1 className="text-lg lg:text-xl mb-4 font-serif">Model download required</h1>
          <p className="font-sans text-sm text-[#878787]">
            SpeechCraft needs Whisper{" "}
            <span className="text-foreground">{downloadTarget.model}</span> before speaker
            detection can start. Nothing is downloaded until you continue.
          </p>
        </div>
        <div className="mt-10 lg:mt-12">
          <SubmitButton
            type="button"
            onClick={() => void beginDownload()}
            className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
            isSubmitting={submitting}
          >
            <div className="flex items-center justify-center gap-2">
              <Icons.HuggingFace size={16} />
              <span>Download &amp; Continue</span>
            </div>
          </SubmitButton>
        </div>
      </>
    );
  }

  if (phase === "needs_authorization") {
    return (
      <>
        <div className="text-center space-y-2">
          <h1 className="text-lg lg:text-xl mb-4 font-serif">Authorize pyannote Community-1</h1>
          <p className="font-sans text-sm text-[#878787]">
            Accept the model conditions and provide a Hugging Face read token to continue setup. SpeechCraft sends it to the local controller for this installation only.
          </p>
        </div>
        <ol className="mt-5 list-inside list-decimal space-y-2 text-center text-sm text-[#878787]">
          <li><a className="underline underline-offset-2" href="https://huggingface.co/pyannote/speaker-diarization-community-1" target="_blank" rel="noreferrer">Accept Community-1 conditions</a></li>
          <li><a className="underline underline-offset-2" href="https://huggingface.co/settings/tokens" target="_blank" rel="noreferrer">Create a read-access token</a></li>
        </ol>
        <form className="mt-8 flex flex-col gap-3" onSubmit={submitPyannoteAuthorization}>
          <label className="sr-only" htmlFor="speechcraft-hf-token">Hugging Face token</label>
          <input
            id="speechcraft-hf-token"
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={hfToken}
            onChange={(event) => setHfToken(event.target.value)}
            placeholder="hf_…"
            className="h-10 w-full rounded-md border border-white/15 bg-transparent px-3 text-sm outline-none focus:border-white/40"
          />
          {authorizationError && <p role="alert" className="text-sm text-[#c98b8b]">{authorizationError}</p>}
          <SubmitButton
            type="submit"
            disabled={!hfToken.trim()}
            className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
            isSubmitting={submitting}
          >
            <span>Verify &amp; Continue</span>
          </SubmitButton>
        </form>
      </>
    );
  }

  if (progressView && (phase === "downloading" || phase === "loading" || phase === "starting")) {
    return <RestrainedProgress view={progressView} />;
  }

  return (
    <>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Preparing models</h1>
        <p className="font-sans text-sm text-[#878787]">
          {phase === "checking"
            ? "Checking whether a Whisper model is already available…"
            : model
              ? `Using ${model} · starting speaker detection…`
              : "Detecting your hardware and loading the best Whisper model…"}
        </p>
      </div>
      <div className="mt-10 lg:mt-12 flex items-center justify-center">
        <Spinner size={20} />
      </div>
    </>
  );
}
