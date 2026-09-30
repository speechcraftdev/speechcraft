"use client";

import { Button } from "@midday/ui/button";
import { useToast } from "@midday/ui/use-toast";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { RiskiestKeptTable, RandomKeptTable } from "./boundary-tables";
import {
  combinedSummary,
  thresholdImpactCurve,
  type HumanLabeledClip,
  type QcClip,
} from "./qc-logic";
import { fetchDatasetQc, resolveMediaUrl, type DatasetQcClipApi, type QcSubset } from "./speechcraft-api";
import { SpeechcraftApiError } from "./speechcraft-write-api";
import { ThresholdImpactChart, type HumanLabelMarker } from "./threshold-impact-chart";

function toQcClip(api: DatasetQcClipApi): QcClip {
  return {
    clipId: api.clip_id,
    audioUrl: resolveMediaUrl(api.audio_url),
    durationSec: api.duration_sec,
    transcriptMatch: api.transcript_match,
    speakerCheck: api.speaker_check,
    reasonCodes: [
      ...api.transcript_reason_codes,
      ...api.speaker_reason_codes,
      ...api.candidate_reason_codes,
      ...api.qc_reason_codes,
    ],
    trainingText: api.training_text,
  };
}

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  return mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
}

const DEMO_CLIPS: QcClip[] = Array.from({ length: 140 }, (_, i) => {
  const seed = (i * 9301 + 49297) % 233280;
  const rand = seed / 233280;
  const transcriptMatch = Math.max(0, Math.min(100, 60 + rand * 45 - 10));
  const speakerCheck = Math.max(0, Math.min(100, 55 + ((seed * 3) % 233280) / 233280 * 45));
  return {
    clipId: `demo-clip-${i + 1}`,
    durationSec: 3 + rand * 9,
    transcriptMatch: i % 17 === 0 ? null : Number(transcriptMatch.toFixed(1)),
    speakerCheck: i % 23 === 0 ? null : Number(speakerCheck.toFixed(1)),
    reasonCodes: [],
    trainingText: "The quick brown fox jumps over the lazy dog near the riverbank.",
  };
});

export function DatasetHealthPage({
  runId,
  demo = false,
  humanLabeledClips = [],
  activeQcSubset,
  draftQcThresholds,
  onDraftQcThresholdsChange,
  onOpenInClipLab,
}: {
  runId: string | null;
  demo?: boolean;
  humanLabeledClips?: HumanLabeledClip[];
  activeQcSubset: QcSubset | null;
  draftQcThresholds: { transcript_match_min: number; speaker_check_min: number };
  onDraftQcThresholdsChange: (next: { transcript_match_min: number; speaker_check_min: number }) => void;
  onOpenInClipLab: (thresholds: { transcript_match_min: number; speaker_check_min: number }) => Promise<void>;
}) {
  const { toast } = useToast();
  const [opening, setOpening] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["sc-dataset-qc", runId],
    queryFn: () => fetchDatasetQc(runId!),
    enabled: !demo && !!runId,
    staleTime: 30_000,
  });

  const clips = useMemo(
    () => (demo ? DEMO_CLIPS : (data?.clips ?? []).map(toQcClip)),
    [demo, data],
  );

  const transcriptThreshold = draftQcThresholds.transcript_match_min;
  const speakerThreshold = draftQcThresholds.speaker_check_min;
  const subsetIsOpen = Boolean(
    activeQcSubset &&
      activeQcSubset.transcript_match_min === transcriptThreshold &&
      activeQcSubset.speaker_check_min === speakerThreshold,
  );

  const transcriptCurve = useMemo(
    () => thresholdImpactCurve(clips, (c) => c.transcriptMatch),
    [clips],
  );
  const speakerCurve = useMemo(
    () => thresholdImpactCurve(clips, (c) => c.speakerCheck),
    [clips],
  );

  const transcriptMarkers = useMemo<HumanLabelMarker[]>(
    () =>
      humanLabeledClips
        .filter((clip) => clip.transcriptMatch != null)
        .map((clip) => ({
          clipId: clip.clipId,
          score: clip.transcriptMatch as number,
          status: clip.status,
          cleanAccepted: clip.cleanAccepted,
        })),
    [humanLabeledClips],
  );
  const speakerMarkers = useMemo<HumanLabelMarker[]>(
    () =>
      humanLabeledClips
        .filter((clip) => clip.speakerCheck != null)
        .map((clip) => ({
          clipId: clip.clipId,
          score: clip.speakerCheck as number,
          status: clip.status,
          cleanAccepted: clip.cleanAccepted,
        })),
    [humanLabeledClips],
  );

  const summary = useMemo(
    () => combinedSummary(clips, transcriptThreshold, speakerThreshold),
    [clips, transcriptThreshold, speakerThreshold],
  );

  const ready = demo || (data?.ready ?? false);
  const qcNotReady = !demo && data && !data.ready;

  const handleOpen = async () => {
    if (subsetIsOpen) return;
    setOpening(true);
    try {
      await onOpenInClipLab({
        transcript_match_min: transcriptThreshold,
        speaker_check_min: speakerThreshold,
      });
      toast({
        title: "Opened in Clip Lab",
        description: `${summary.acceptedCount} clips pass both QC gates at these thresholds.`,
        variant: "success",
        duration: 4000,
      });
    } catch (err) {
      toast({
        title: "Could not open Clip Lab",
        description: err instanceof SpeechcraftApiError ? err.detail : String(err),
        variant: "error",
        duration: 4500,
      });
    } finally {
      setOpening(false);
    }
  };

  if (!demo && !runId) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-muted-foreground">No dataset run selected yet.</p>
      </div>
    );
  }

  if (!demo && isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
        Loading dataset health…
      </div>
    );
  }

  if (!demo && error) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="max-w-sm text-center">
          <p className="font-serif text-xl">Couldn't load QC</p>
          <p className="mt-2 text-sm text-muted-foreground">
            {error instanceof SpeechcraftApiError ? error.detail : String(error)}
          </p>
        </div>
      </div>
    );
  }

  if (qcNotReady) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <div className="max-w-sm text-center">
          <p className="font-serif text-xl">QC hasn't run yet</p>
          <p className="mt-2 text-sm text-muted-foreground">
            {data?.missing_artifacts.length
              ? `Missing: ${data.missing_artifacts.join(", ")}.`
              : "Run transcript and speaker QC for this run to see dataset health."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-[1400px] p-6">
        <div className="mb-6 flex items-center justify-between">
          <div className="font-serif text-lg leading-none">
            {summary.acceptedCount} of {clips.length} clips pass both gates at these thresholds ·{" "}
            {formatDuration(summary.acceptedDurationSec)}
            {activeQcSubset ? <span className="ml-2 text-xs text-[#878787]">(active subset)</span> : null}
          </div>
          <Button
            type="button"
            size="sm"
            variant={subsetIsOpen ? "secondary" : "default"}
            className={subsetIsOpen
              ? "h-8 border border-[#3a3a3a] !bg-[#1b1b1b] !text-white hover:!bg-[#1b1b1b]"
              : "h-8"}
            disabled={(!demo && !runId) || opening}
            onClick={() => void handleOpen()}
            aria-pressed={subsetIsOpen}
          >
            {opening ? "Opening…" : "Open in Clip Lab"}
          </Button>
        </div>

        <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
          <ThresholdImpactChart
            title="Transcript match"
            points={transcriptCurve}
            threshold={transcriptThreshold}
            onThresholdChange={(value) => onDraftQcThresholdsChange({
              transcript_match_min: value,
              speaker_check_min: speakerThreshold,
            })}
            humanLabels={transcriptMarkers}
          />
          <ThresholdImpactChart
            title="Speaker check"
            points={speakerCurve}
            threshold={speakerThreshold}
            onThresholdChange={(value) => onDraftQcThresholdsChange({
              transcript_match_min: transcriptThreshold,
              speaker_check_min: value,
            })}
            humanLabels={speakerMarkers}
          />
        </div>

        <div className="mt-6 grid grid-cols-1 gap-6 md:grid-cols-2">
          <RiskiestKeptTable
            clips={clips}
            transcriptThreshold={transcriptThreshold}
            speakerThreshold={speakerThreshold}
          />
          <RandomKeptTable
            clips={clips}
            transcriptThreshold={transcriptThreshold}
            speakerThreshold={speakerThreshold}
          />
        </div>
      </div>

    </div>
  );
}
