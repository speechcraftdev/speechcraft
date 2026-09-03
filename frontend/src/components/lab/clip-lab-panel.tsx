"use client";

import { Button } from "@midday/ui/button";
import { cn } from "@midday/ui/cn";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@midday/ui/dropdown-menu";
import { Icons } from "@midday/ui/icons";
import { Separator } from "@midday/ui/separator";
import { useToast } from "@midday/ui/use-toast";
import { useEffect, useRef, useState } from "react";
import {
  WaveformEditor,
  type WaveformEditorHandle,
} from "./clip-lab-editor/waveform-editor";
import { type ClipEdit, type LabClip, formatClock, formatSeconds } from "./lab-data";
import type { DatasetAudioEditOperation } from "./speechcraft-write-api";

const PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;
const DEFAULT_PLAYBACK_RATE = 1;

type ClipLabPanelProps = {
  clip: LabClip;
  canUndo: boolean;
  canRedo: boolean;
  onAccept: () => void;
  onReject: () => void;
  onAppendEdit: (edit: ClipEdit) => void;
  onCommitAudioOp: (op: DatasetAudioEditOperation) => Promise<boolean>;
  onUndo: () => void;
  onRedo: () => void;
  onMarkReference: () => void;
  onRunModel: () => void;
  autoplay: boolean;
  onAutoplayConsumed: () => void;
};

export function ClipLabPanel({
  clip,
  canUndo,
  canRedo,
  onAccept,
  onReject,
  onAppendEdit,
  onCommitAudioOp,
  onUndo,
  onRedo,
  onMarkReference,
  onRunModel,
  autoplay,
  onAutoplayConsumed,
}: ClipLabPanelProps) {
  const { toast } = useToast();
  const editorRef = useRef<WaveformEditorHandle | null>(null);
  const selectionReadoutRef = useRef<HTMLSpanElement | null>(null);
  const timeReadoutRef = useRef<HTMLSpanElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [rate, setRate] = useState(DEFAULT_PLAYBACK_RATE);
  const [hasSelection, setHasSelection] = useState(false);
  const [editInFlight, setEditInFlight] = useState(false);

  const duration = clip.durationSeconds;

  useEffect(() => {
    setIsPlaying(false);
    setRate(DEFAULT_PLAYBACK_RATE);
    setHasSelection(false);
    setEditInFlight(false);
    editorRef.current?.setPlaybackRate(DEFAULT_PLAYBACK_RATE);
  }, [clip.id]);

  const chooseRate = (value: string) => {
    const next = Number(value);
    if (!PLAYBACK_RATES.includes(next as (typeof PLAYBACK_RATES)[number])) return;
    setRate(next);
    editorRef.current?.setPlaybackRate(next);
  };

  const splitClip = () => {
    editorRef.current?.stopPlayback();
    onAppendEdit({ op: "split", startSeconds: Number((duration / 2).toFixed(2)) });
  };
  const mergeClip = () => {
    editorRef.current?.stopPlayback();
    onAppendEdit({ op: "merge_next" });
  };

  const expectedDurationSamples =
    clip.sampleRateHz > 0 ? Math.round(clip.durationSeconds * clip.sampleRateHz) : null;

  return (
    <section className="flex flex-col border border-border">
      <div className="flex h-11 items-center justify-between border-b border-border px-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Clip Lab
        </span>
        <div className="flex items-center gap-3 text-xs tabular-nums text-muted-foreground">
          <span>{formatSeconds(duration)}</span>
          <span>{(clip.sampleRateHz / 1000).toFixed(0)} kHz</span>
          <span>{clip.variant}</span>
        </div>
      </div>

      <div className="flex items-center gap-2 px-4 pt-4">
        <Button
          type="button"
          onClick={onAccept}
          className="h-9 flex-1 bg-emerald-600 text-white hover:bg-emerald-600/90"
        >
          Accept &amp; Next
        </Button>
        <Button type="button" variant="destructive" onClick={onReject} className="h-9 flex-1">
          Reject &amp; Next
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="outline" size="icon" className="h-9 w-9">
              <Icons.MoreVertical className="size-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onMarkReference}>
              Mark as reference candidate
            </DropdownMenuItem>
            <DropdownMenuItem onClick={onRunModel}>Run DeepFilterNet</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <div className="px-4 py-4">
        {clip.audioUrl ? (
          <WaveformEditor
            key={clip.id}
            ref={editorRef}
            audioUrl={clip.audioUrl}
            sampleRateHz={clip.sampleRateHz}
            expectedDurationSamples={expectedDurationSamples}
            playbackRate={rate}
            autoplay={autoplay}
            onAutoplayConsumed={onAutoplayConsumed}
            statusRefs={{ selection: selectionReadoutRef, time: timeReadoutRef }}
            onPlayingChange={setIsPlaying}
            onHasSelectionChange={setHasSelection}
            onEditInFlightChange={setEditInFlight}
            onCommitAudioOp={onCommitAudioOp}
            onRefuseEntireClip={() => {
              toast({
                title: "Cannot delete entire clip",
                description: "Leave some audio in the clip or reject it instead.",
                variant: "error",
                duration: 3000,
              });
            }}
          />
        ) : (
          <div className="flex h-[150px] items-center justify-center bg-secondary/40 text-sm text-muted-foreground">
            No audio for this clip.
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1 border-t border-border px-3 py-2.5">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={() => editorRef.current?.togglePlayback()}
        >
          {isPlaying ? <Icons.Pause className="size-4" /> : <Icons.Play className="size-4" />}
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button type="button" variant="ghost" size="sm" className="h-8 tabular-nums">
              Speed {rate}×
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-24">
            <DropdownMenuRadioGroup value={String(rate)} onValueChange={chooseRate}>
              {PLAYBACK_RATES.map((playbackRate) => (
                <DropdownMenuRadioItem key={playbackRate} value={String(playbackRate)}>
                  {playbackRate}×
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
          </DropdownMenuContent>
        </DropdownMenu>

        <Separator orientation="vertical" className="mx-1 h-5" />

        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8"
          onClick={onUndo}
          disabled={!canUndo || editInFlight}
        >
          Undo
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8"
          onClick={onRedo}
          disabled={!canRedo || editInFlight}
        >
          Redo
        </Button>

        <Separator orientation="vertical" className="mx-1 h-5" />

        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={splitClip}>
          Split
        </Button>
        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={mergeClip}>
          Merge next
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8"
          onClick={() => void editorRef.current?.insertSilenceAtCursor()}
          disabled={editInFlight || !clip.audioUrl}
        >
          Insert silence
        </Button>
        {hasSelection ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8"
            onClick={() => void editorRef.current?.deleteSelection()}
            disabled={editInFlight}
          >
            Delete selection
          </Button>
        ) : null}

        <div className="ml-auto flex items-center gap-3 pr-1 text-xs tabular-nums text-muted-foreground">
          <span ref={selectionReadoutRef} className={cn(hasSelection && "text-foreground")}>
            Sel none
          </span>
          <span ref={timeReadoutRef} className="text-foreground">
            {formatClock(0)} / {formatClock(duration)}
          </span>
        </div>
      </div>
    </section>
  );
}
