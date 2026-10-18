import { ProgressTelemetry } from "./setup-api";

function formatBytes(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  if (value < 1_000) return `${Math.round(value)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value;
  let unit = -1;
  do { amount /= 1_000; unit += 1; } while (amount >= 1_000 && unit < units.length - 1);
  return `${amount.toFixed(amount >= 100 ? 0 : amount >= 10 ? 1 : 2)} ${units[unit]}`;
}

function phaseLabel(phase: string | null | undefined) {
  return ({ queued: "Queued", inspecting: "Inspecting", downloading: "Downloading", verifying: "Verifying files",
    verified: "Verified", installing: "Installing", running_inference: "Running inference",
    interrupted: "Download interrupted", complete: "Complete" } as Record<string, string>)[phase || ""] || "Working";
}

function formatDuration(seconds: number | null | undefined) {
  if (!Number.isSafeInteger(seconds) || seconds! < 0) return null;
  if (seconds! === 0) return "Less than a second";
  if (seconds! < 60) return `About ${seconds!} sec left`;
  const minutes = Math.ceil(seconds! / 60);
  if (minutes < 60) return `About ${minutes} min left`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder ? `About ${hours} hr ${remainder} min left` : `About ${hours} hr left`;
}

export function ProgressDetails({ progress, fallbackName }: { progress: ProgressTelemetry | null | undefined; fallbackName: string }) {
  if (!progress) return null;
  const phase = progress.phase;
  const isDownload = phase === "downloading";
  const downloadedBytes = progress.downloaded_bytes ?? progress.current ?? null;
  const totalBytes = progress.total_bytes ?? progress.total ?? null;
  const downloaded = formatBytes(downloadedBytes);
  const total = formatBytes(totalBytes);
  const calculatedPercent = typeof downloadedBytes === "number" && typeof totalBytes === "number" && totalBytes > 0
    ? Math.floor(downloadedBytes * 100 / totalBytes) : null;
  const reportedPercent = typeof progress.percent === "number" && Number.isFinite(progress.percent) ? progress.percent : null;
  const percent = isDownload && downloaded && total && (calculatedPercent !== null || reportedPercent !== null)
    ? Math.max(0, Math.min(100, calculatedPercent ?? reportedPercent ?? 0)) : null;
  const speed = isDownload && !progress.transfer_indeterminate && !progress.indeterminate && !progress.stalled
    && typeof progress.bytes_per_second === "number" && progress.bytes_per_second > 0
    ? `${formatBytes(progress.bytes_per_second)}/s` : null;
  const eta = isDownload && !progress.transfer_indeterminate && !progress.indeterminate && !progress.stalled
    ? formatDuration(progress.eta_seconds) : null;
  const fileCount = isDownload && Number.isSafeInteger(progress.files_completed) && Number.isSafeInteger(progress.files_total)
    ? `${progress.files_completed} / ${progress.files_total} files completed` : null;
  const label = progress.model_name || fallbackName;
  const ariaLabel = `${label}: ${phaseLabel(phase).toLowerCase()}${percent === null ? "" : ` ${percent}%`}`;
  return <div className="mt-3 space-y-2 text-sm" aria-live="polite">
    <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
      <span className="font-medium">{label}</span>
      <span className="text-[#777]">{phaseLabel(phase)}</span>
    </div>
    {progress.current_filename && isDownload && <p className="truncate text-xs text-[#878787]">{progress.current_filename}</p>}
    {isDownload && downloaded && total && <div className="flex items-baseline justify-between gap-3 text-xs text-[#777]">
      <span>{downloaded} / {total}</span><span>{percent}%</span>
    </div>}
    {isDownload && downloaded && !total && <p className="text-xs text-[#777]">Downloaded: {downloaded}</p>}
    {isDownload && !downloaded && total && <p className="text-xs text-[#777]">Total model size: {total}</p>}
    {fileCount && <p className="text-xs text-[#777]">{fileCount}</p>}
    {isDownload && <div role="progressbar" aria-label={ariaLabel} aria-valuemin={0} aria-valuemax={100}
      {...(percent === null ? {} : { "aria-valuenow": percent })}
      className="h-2 overflow-hidden rounded-full bg-black/10 dark:bg-white/10">
      <div className={`h-full bg-[#444] dark:bg-[#ddd] ${percent === null ? "w-1/3 animate-pulse" : "transition-[width]"}`}
        style={percent === null ? undefined : { width: `${percent}%` }} />
    </div>}
    {isDownload && <div className="flex flex-wrap justify-between gap-x-4 gap-y-1 text-xs text-[#777]">
      <span>Speed: {progress.stalled ? "Stalled" : speed || "unavailable"}</span>
      <span>ETA: {progress.stalled ? "Unavailable" : eta || (progress.transfer_indeterminate || progress.indeterminate ? "unavailable" : "Calculating…")}</span>
    </div>}
    {!isDownload && <p className="text-xs text-[#777]">Setup is working. Download progress is unavailable for this phase.</p>}
  </div>;
}
