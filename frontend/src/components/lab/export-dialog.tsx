"use client";

import { Button } from "@midday/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@midday/ui/dialog";
import { Label } from "@midday/ui/label";
import { Separator } from "@midday/ui/separator";
import { Spinner } from "@midday/ui/spinner";
import { useToast } from "@midday/ui/use-toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { fetchCanonicalExportPreview, fetchCanonicalExports, type CanonicalExportSummary } from "./speechcraft-api";
import { createCanonicalExport, SpeechcraftApiError } from "./speechcraft-write-api";

type ExportDialogProps = {
  runId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs.toFixed(0)}s`;
}

function formatExportDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

async function copyText(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // Try the legacy selection-based fallback below.
  }

  try {
    const input = document.createElement("textarea");
    input.value = value;
    input.setAttribute("readonly", "");
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    const copied = document.execCommand("copy");
    input.remove();
    return copied;
  } catch {
    return false;
  }
}

export function ExportDialog({ runId, open, onOpenChange }: ExportDialogProps) {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [isExporting, setIsExporting] = useState(false);
  const [copyingExportId, setCopyingExportId] = useState<string | null>(null);

  const {
    data: preview,
    isLoading: previewLoading,
    error: previewError,
  } = useQuery({
    queryKey: ["sc-canonical-export-preview", runId],
    queryFn: () => fetchCanonicalExportPreview(runId!),
    enabled: open && !!runId,
    staleTime: 0,
  });

  const {
    data: exports = [],
    isLoading: exportsLoading,
    error: exportsError,
  } = useQuery({
    queryKey: ["sc-canonical-exports", runId],
    queryFn: () => fetchCanonicalExports(runId!),
    enabled: open && !!runId,
    staleTime: 0,
  });

  const exportBlocked =
    !preview ||
    preview.accepted_clip_count === 0 ||
    preview.blocked_clip_count > 0;

  const startExport = async () => {
    if (!runId || exportBlocked) return;
    setIsExporting(true);
    try {
      const summary = await createCanonicalExport(runId);
      onOpenChange(false);
      const clipSummary = `${summary.accepted_clip_count} clips (${formatDuration(summary.total_duration_sec)})`;
      const copied = await copyText(summary.manifest_path);
      toast({
        title: "Export complete",
        description: copied
          ? `${clipSummary}. JSONL path copied to clipboard: ${summary.manifest_path}`
          : `${clipSummary}. Couldn't copy the JSONL path: ${summary.manifest_path}`,
        variant: "success",
        duration: 8000,
      });
      void queryClient.invalidateQueries({ queryKey: ["sc-canonical-export-preview", runId] });
      void queryClient.invalidateQueries({ queryKey: ["sc-canonical-exports", runId] });
    } catch (err) {
      const description =
        err instanceof SpeechcraftApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : "Export failed";
      toast({
        title: "Export failed",
        description,
        variant: "error",
        duration: 6000,
      });
    } finally {
      setIsExporting(false);
    }
  };

  const copyExportPath = async (item: CanonicalExportSummary) => {
    setCopyingExportId(item.export_id);
    const copied = await copyText(item.manifest_path);
    setCopyingExportId(null);
    toast({
      title: copied ? "Path copied" : "Couldn't copy path",
      description: item.manifest_path,
      variant: copied ? "success" : "error",
      duration: 4000,
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[900px]">
        <div className="p-4">
          <DialogHeader className="mb-6">
            <DialogTitle className="font-serif text-lg">Export dataset</DialogTitle>
            <DialogDescription>
              Compiles accepted Clip Lab slices and their final transcripts into a
              canonical JSONL manifest. Only human-accepted clips are included.
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
            <div className="space-y-4">
              <div className="space-y-1.5">
              <Label className="text-xs font-normal uppercase tracking-wide text-[#878787]">
                Format
              </Label>
              <p className="text-sm">JSONL manifest</p>
              <p className="text-xs text-[#878787]">
                Writes <span className="font-mono">speechcraft_dataset.jsonl</span> — one JSON
                line per accepted clip (audio path, transcript, review metadata).
              </p>
              </div>

              <Separator />

              {!runId ? (
              <p className="text-sm text-muted-foreground">
                Open a dataset run in Clip Lab to export.
              </p>
              ) : previewLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Spinner className="size-4" />
                <span>Checking export readiness…</span>
              </div>
            ) : previewError ? (
              <p className="text-sm text-destructive">
                {previewError instanceof Error
                  ? previewError.message
                  : "Could not load export preview."}
              </p>
            ) : preview ? (
              <div className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <p className="text-xs text-[#878787]">Accepted clips</p>
                    <p className="tabular-nums">{preview.accepted_clip_count}</p>
                  </div>
                  <div>
                    <p className="text-xs text-[#878787]">Total duration</p>
                    <p className="tabular-nums">{formatDuration(preview.total_duration_sec)}</p>
                  </div>
                  <div>
                    <p className="text-xs text-[#878787]">Original audio</p>
                    <p className="tabular-nums">{preview.original_audio_count}</p>
                  </div>
                  <div>
                    <p className="text-xs text-[#878787]">Edited audio</p>
                    <p className="tabular-nums">{preview.edited_audio_count}</p>
                  </div>
                </div>

                {preview.accepted_clip_count === 0 ? (
                  <p className="text-xs text-muted-foreground">
                    Accept at least one clip in Clip Lab before exporting.
                  </p>
                ) : null}

                {preview.blocked_clip_count > 0 ? (
                  <div className="space-y-2 border border-destructive/40 bg-destructive/5 p-3">
                    <p className="text-xs font-medium text-destructive">
                      {preview.blocked_clip_count} accepted clip
                      {preview.blocked_clip_count === 1 ? "" : "s"} blocked
                    </p>
                    <ul className="max-h-28 space-y-1 overflow-y-auto text-xs text-muted-foreground">
                      {preview.blocked_reasons.map((item) => (
                        <li key={item.clip_id}>
                          <span className="font-mono">{item.clip_id}</span>:{" "}
                          {item.reasons.join(", ")}
                        </li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
              ) : null}

              <Separator />

              <div className="flex justify-end gap-2">
                <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                  Cancel
                </Button>
                <Button
                  type="button"
                  onClick={() => void startExport()}
                  disabled={!runId || isExporting || previewLoading || exportBlocked}
                >
                  {isExporting ? (
                    <div className="flex items-center space-x-2">
                      <Spinner className="size-4" />
                      <span>Exporting…</span>
                    </div>
                  ) : (
                    <span>Export</span>
                  )}
                </Button>
              </div>
            </div>

            <div className="space-y-3 border-t border-border pt-4 md:border-l md:border-t-0 md:pl-6 md:pt-0">
              <div>
                <h3 className="text-sm font-medium">Export history</h3>
                <p className="mt-1 text-xs text-muted-foreground">
                  JSONL manifests created for this dataset run.
                </p>
              </div>
              {!runId ? (
                <p className="text-sm text-muted-foreground">Open a dataset run to view exports.</p>
              ) : exportsLoading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Spinner className="size-4" />
                  <span>Loading export history…</span>
                </div>
              ) : exportsError ? (
                <p className="text-sm text-destructive">Could not load export history.</p>
              ) : exports.length === 0 ? (
                <p className="text-sm text-muted-foreground">No exports yet.</p>
              ) : (
                <div className="max-h-[360px] space-y-2 overflow-y-auto pr-1">
                  {exports.map((item) => (
                    <div key={item.export_id} className="border border-border p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <p className="text-xs text-muted-foreground">
                            {formatExportDate(item.created_at)}
                          </p>
                          <p className="mt-1 break-all font-mono text-xs">{item.manifest_path}</p>
                        </div>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="shrink-0"
                          disabled={copyingExportId !== null}
                          onClick={() => void copyExportPath(item)}
                        >
                          {copyingExportId === item.export_id ? "Copying…" : "Copy"}
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
