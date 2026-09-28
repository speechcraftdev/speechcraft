"use client";

import { Badge } from "@midday/ui/badge";
import { Button } from "@midday/ui/button";
import { cn } from "@midday/ui/cn";
import { Progress } from "@midday/ui/progress";
import { Separator } from "@midday/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@midday/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@midday/ui/tooltip";
import { useEffect, useState } from "react";
import {
  type LabClip,
  type ReviewStatus,
  REASON_CODE_LABELS,
  REVIEW_STATUS_ORDER,
  STATUS_LABELS,
  formatDurationCompact,
  formatSeconds,
} from "./lab-data";

type Stats = {
  total: number;
  reviewed: number;
  predictedClipCount: number | null;
  predictedDurationSeconds: number | null;
  rows: Array<{ label: string; clips: number; durationSeconds: number }>;
};

type InspectorRailProps = {
  clip: LabClip;
  stats: Stats;
  onStatusChange: (status: ReviewStatus) => void;
  onSaveReference: () => Promise<string | null>;
};

function StatRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between py-1 text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">{value}</span>
    </div>
  );
}

export function InspectorRail({
  clip,
  stats,
  onStatusChange,
  onSaveReference,
}: InspectorRailProps) {
  const reviewedPercent = stats.total > 0 ? (stats.reviewed / stats.total) * 100 : 0;
  const [isSavingReference, setIsSavingReference] = useState(false);
  const [referenceFolderPath, setReferenceFolderPath] = useState<string | null>(null);

  useEffect(() => {
    setIsSavingReference(false);
    setReferenceFolderPath(null);
  }, [clip.id]);

  const saveReference = async () => {
    if (isSavingReference) return;
    setIsSavingReference(true);
    try {
      const folderPath = await onSaveReference();
      setReferenceFolderPath(folderPath);
    } finally {
      setIsSavingReference(false);
    }
  };

  return (
    <TooltipProvider delayDuration={100}>
      <aside className="flex h-full w-[340px] flex-shrink-0 flex-col overflow-y-auto border-l border-border">
        <div className="border-b border-border p-4">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Inspector
          </p>
          <h2 className="mt-0.5 font-serif text-lg">Clip Review</h2>
        </div>

        {/* Always visible: live status + control */}
        <div className="space-y-4 border-b border-border p-4">
          <div className="flex items-center justify-between">
            <span className="text-xs uppercase tracking-wide text-muted-foreground">
              Live status
            </span>
          </div>

          <div className="grid grid-cols-2 gap-1.5">
            {REVIEW_STATUS_ORDER.map((status) => (
              <Button
                key={status}
                type="button"
                size="sm"
                variant={clip.status === status ? "default" : "outline"}
                className="h-8 justify-center text-xs"
                onClick={() => onStatusChange(status)}
              >
                {STATUS_LABELS[status]}
              </Button>
            ))}
          </div>

          {clip.reasonCodes.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {clip.reasonCodes.map((code) => (
                <Tooltip key={code}>
                  <TooltipTrigger asChild>
                    <Badge variant="outline" className="cursor-default text-destructive">
                      {REASON_CODE_LABELS[code] ?? code}
                    </Badge>
                  </TooltipTrigger>
                  <TooltipContent side="left" className="max-w-56">
                    <p className="text-xs">
                      QC flagged this clip: {REASON_CODE_LABELS[code] ?? code}.
                    </p>
                  </TooltipContent>
                </Tooltip>
              ))}
            </div>
          ) : null}
        </div>

        {/* Always visible: compact stats + progress */}
        <div className="space-y-3 border-b border-border p-4">
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="text-[11px] uppercase tracking-wide text-muted-foreground">
                <th className="pb-2 text-left font-medium" />
                <th className="pb-2 text-right font-medium">Clips</th>
                <th className="pb-2 text-right font-medium">Duration</th>
              </tr>
            </thead>
            <tbody>
              {stats.rows.map((row) => (
                <tr key={row.label}>
                  <th className="py-1.5 text-left font-normal text-muted-foreground">{row.label}</th>
                  <td className="py-1.5 text-right tabular-nums">{row.clips}</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {formatDurationCompact(row.durationSeconds)}
                  </td>
                </tr>
              ))}
              <tr>
                <th className="pt-2 text-left font-normal text-muted-foreground">Predicted size</th>
                <td className="pt-2 text-right tabular-nums">
                  {stats.predictedClipCount === null ? "—" : Math.round(stats.predictedClipCount)}
                </td>
                <td className="pt-2 text-right tabular-nums">
                  {stats.predictedDurationSeconds === null
                    ? "—"
                    : formatDurationCompact(stats.predictedDurationSeconds)}
                </td>
              </tr>
            </tbody>
          </table>
          <Separator />
          <div>
            <div className="mb-1.5 flex items-center justify-between text-sm">
              <span className="text-muted-foreground">Review progress</span>
              <span className="tabular-nums">{Math.round(reviewedPercent)}%</span>
            </div>
            <Progress value={reviewedPercent} className="h-2" />
          </div>
          <div className="space-y-2 border-t border-border pt-3">
            <Button
              type="button"
              variant="outline"
              className="w-full"
              disabled={isSavingReference}
              onClick={() => void saveReference()}
            >
              {isSavingReference ? "Saving reference clip…" : "Save as reference clip candidate"}
            </Button>
            {referenceFolderPath ? (
              <p className="break-words text-xs text-muted-foreground" aria-live="polite">
                Saved at <span className="font-mono">{referenceFolderPath}</span>
              </p>
            ) : null}
          </div>
        </div>

        {/* Deep tooling behind tabs */}
        <div className="p-4">
          <Tabs defaultValue="edits">
            <TabsList className="grid w-full grid-cols-2">
              <TabsTrigger value="edits">Edits</TabsTrigger>
              <TabsTrigger value="provenance">Source</TabsTrigger>
            </TabsList>

            <TabsContent value="edits" className="space-y-2">
              {clip.originalTranscript !== clip.transcript ? (
                <div className="border border-border p-3 text-sm">
                  <div className="font-medium">Transcript</div>
                  <div className="mt-2 space-y-2 text-xs">
                    <div>
                      <span className="text-muted-foreground">Original</span>
                      <p className="mt-0.5 whitespace-pre-wrap">
                        {clip.originalTranscript || "(blank transcript)"}
                      </p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Edited</span>
                      <p className="mt-0.5 whitespace-pre-wrap">
                        {clip.transcript || "(blank transcript)"}
                      </p>
                    </div>
                  </div>
                </div>
              ) : null}
              {clip.edits.length > 0 ? (
                clip.edits.map((edit, index) => (
                  <div
                    key={`${edit.op}-${index}`}
                    className="flex items-center justify-between border border-border p-3 text-sm"
                  >
                    <span className="font-medium">{edit.op.replace(/_/g, " ")}</span>
                    <span className="text-xs tabular-nums text-muted-foreground">
                      {edit.startSeconds !== undefined
                        ? edit.endSeconds !== undefined
                          ? `${formatSeconds(edit.startSeconds)}–${formatSeconds(edit.endSeconds)}`
                          : formatSeconds(edit.startSeconds)
                        : edit.durationSeconds !== undefined
                          ? formatSeconds(edit.durationSeconds)
                          : ""}
                    </span>
                  </div>
                ))
              ) : clip.originalTranscript === clip.transcript ? (
                <p className="py-4 text-sm text-muted-foreground">
                  No transcript or waveform edits on this clip yet.
                </p>
              ) : null}
            </TabsContent>

            <TabsContent value="provenance" className="space-y-2">
              <StatRow label="Source" value={clip.sourceRecording} />
              <StatRow label="Path" value={clip.path ?? "—"} />
              <StatRow label="Speaker" value={clip.speaker} />
              <StatRow label="Language" value={clip.language} />
              <StatRow
                label="Original range"
                value={`${formatSeconds(clip.originalStartSeconds)}–${formatSeconds(clip.originalEndSeconds)}`}
              />
              <StatRow label="Duration" value={formatDurationCompact(clip.durationSeconds)} />
            </TabsContent>

          </Tabs>
        </div>
      </aside>
    </TooltipProvider>
  );
}
