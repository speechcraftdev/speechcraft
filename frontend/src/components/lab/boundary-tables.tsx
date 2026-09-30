"use client";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@midday/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@midday/ui/table";
import { useMemo, useState } from "react";
import {
  evenlyDistributedKept,
  riskiestKept,
  type KeptSort,
  type ManualOverride,
  type QcClip,
} from "./qc-logic";
import { QcMiniWaveform } from "./qc-mini-waveform";

function formatScore(score: number | null): string {
  return score === null ? "—" : score.toFixed(2);
}

function formatMargin(margin: number): string {
  if (!Number.isFinite(margin)) return "—";
  const sign = margin > 0 ? "+" : "";
  return `${sign}${margin.toFixed(2)}`;
}

function ClipPreview({ clip }: { clip: QcClip }) {
  return (
    <div className="min-w-0 space-y-1.5">
      {clip.audioUrl ? (
        <QcMiniWaveform audioUrl={clip.audioUrl} label={clip.trainingText || "this clip"} />
      ) : (
        <span className="text-xs text-muted-foreground">Audio unavailable</span>
      )}
      <span className="block whitespace-normal break-words text-xs leading-relaxed text-muted-foreground">
        {clip.trainingText || "No transcript text recorded."}
      </span>
    </div>
  );
}

type BoundaryTablesProps = {
  clips: QcClip[];
  transcriptThreshold: number;
  speakerThreshold: number;
  overrides?: Record<string, ManualOverride | null | undefined>;
  limit?: number;
};

export function RiskiestKeptTable({
  clips,
  transcriptThreshold,
  speakerThreshold,
  overrides = {},
  limit = 10,
}: BoundaryTablesProps) {
  const [sort, setSort] = useState<KeptSort>("risk");

  const rows = useMemo(
    () =>
      riskiestKept(clips, transcriptThreshold, speakerThreshold, sort, overrides).slice(0, limit),
    [clips, transcriptThreshold, speakerThreshold, sort, overrides, limit],
  );

  return (
    <div className="border border-border">
      <div className="flex items-center justify-between border-b border-border p-4">
        <div>
          <h3 className="font-serif text-lg leading-none">Riskiest kept</h3>
          <p className="mt-1 text-xs text-[#878787]">
            Accepted clips closest to failing — the ones worth a human ear.
          </p>
        </div>
        <Select value={sort} onValueChange={(v) => setSort(v as KeptSort)}>
          <SelectTrigger className="h-8 w-[160px] text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="risk">Sort by risk</SelectItem>
            <SelectItem value="transcript">Closest — transcript</SelectItem>
            <SelectItem value="speaker">Closest — speaker</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {rows.length === 0 ? (
        <p className="p-4 text-sm text-[#878787]">No accepted clips at these thresholds.</p>
      ) : (
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-[68%]">Clip</TableHead>
              <TableHead className="w-[16%] whitespace-nowrap text-right">Transcript</TableHead>
              <TableHead className="w-[16%] whitespace-nowrap text-right">Speaker</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(({ clip, transcriptMargin, speakerMargin }) => (
              <TableRow key={clip.clipId}>
                <TableCell className="max-w-0 align-top">
                  <ClipPreview clip={clip} />
                </TableCell>
                <TableCell className="whitespace-nowrap text-right tabular-nums">
                  {formatScore(clip.transcriptMatch)}
                  <span className="ml-1 text-[#878787]">({formatMargin(transcriptMargin)})</span>
                </TableCell>
                <TableCell className="whitespace-nowrap text-right tabular-nums">
                  {formatScore(clip.speakerCheck)}
                  <span className="ml-1 text-[#878787]">({formatMargin(speakerMargin)})</span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

export function RandomKeptTable({
  clips,
  transcriptThreshold,
  speakerThreshold,
  overrides = {},
  limit = 10,
}: BoundaryTablesProps) {
  const rows = useMemo(
    () =>
      evenlyDistributedKept(clips, transcriptThreshold, speakerThreshold, overrides).slice(0, limit),
    [clips, transcriptThreshold, speakerThreshold, overrides, limit],
  );

  return (
    <div className="border border-border">
      <div className="border-b border-border p-4">
        <div>
          <h3 className="font-serif text-lg leading-none">Random kept</h3>
          <p className="mt-1 text-xs text-[#878787]">
            Ten accepted clips spread across the dataset for a quick spot check.
          </p>
        </div>
      </div>

      {rows.length === 0 ? (
        <p className="p-4 text-sm text-[#878787]">No accepted clips at these thresholds.</p>
      ) : (
        <Table className="table-fixed">
          <TableHeader>
            <TableRow>
              <TableHead className="w-[68%]">Clip</TableHead>
              <TableHead className="w-[16%] whitespace-nowrap text-right">Transcript</TableHead>
              <TableHead className="w-[16%] whitespace-nowrap text-right">Speaker</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map(({ clip, transcriptMargin, speakerMargin }) => (
              <TableRow key={clip.clipId}>
                <TableCell className="max-w-0 align-top">
                  <ClipPreview clip={clip} />
                </TableCell>
                <TableCell className="whitespace-nowrap text-right tabular-nums">
                  {formatScore(clip.transcriptMatch)}
                  <span className="ml-1 text-[#878787]">({formatMargin(transcriptMargin)})</span>
                </TableCell>
                <TableCell className="whitespace-nowrap text-right tabular-nums">
                  {formatScore(clip.speakerCheck)}
                  <span className="ml-1 text-[#878787]">({formatMargin(speakerMargin)})</span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
