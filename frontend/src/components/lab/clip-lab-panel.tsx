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
import { useCallback, useEffect, useRef, useState } from "react";
import type WaveSurfer from "wavesurfer.js";
import { type ClipEdit, type LabClip, formatClock, formatSeconds } from "./lab-data";
import { WaveformPane } from "./waveform-pane";

const PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;
const DEFAULT_PLAYBACK_RATE = 1;

type ClipLabPanelProps = {
  clip: LabClip;
  canUndo: boolean;
  canRedo: boolean;
  onAccept: () => void;
  onReject: () => void;
  onAppendEdit: (edit: ClipEdit) => void;
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
  onUndo,
  onRedo,
  onMarkReference,
  onRunModel,
  autoplay,
  onAutoplayConsumed,
}: ClipLabPanelProps) {
  const waveSurferRef = useRef<WaveSurfer | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [rate, setRate] = useState(DEFAULT_PLAYBACK_RATE);
  const [selectionStart, setSelectionStart] = useState(0);
  const [selectionEnd, setSelectionEnd] = useState(0);
  const [hover, setHover] = useState<number | null>(null);

  const duration = clip.durationSeconds;
  const selLo = Math.min(selectionStart, selectionEnd);
  const selHi = Math.max(selectionStart, selectionEnd);
  const hasSelection = selHi - selLo > 0.02;

  // Reset transient state on clip change or when rendered audio updates.
  useEffect(() => {
    setPlayhead(0);
    setSelectionStart(0);
    setSelectionEnd(0);
    setHover(null);
    setIsPlaying(false);
    setRate(DEFAULT_PLAYBACK_RATE);
    waveSurferRef.current?.setPlaybackRate(DEFAULT_PLAYBACK_RATE, true);
  }, [clip.id, clip.audioUrl, clip.effectiveAudioRevisionKey]);

  const togglePlayback = useCallback(() => {
    const ws = waveSurferRef.current;
    if (!ws) return;
    if (ws.isPlaying()) {
      ws.pause();
      return;
    }
    if (selHi - selLo > 0.02) {
      void ws.play(selLo, selHi);
    } else {
      void ws.play();
    }
  }, [selLo, selHi]);

  const playFromStart = useCallback(() => {
    const ws = waveSurferRef.current;
    if (!ws) return;
    ws.setTime(0);
    setPlayhead(0);
    void ws.play();
  }, []);

  // Space toggles playback; Shift+Space restarts from clip start.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      if (event.code === "Space" && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        if (event.shiftKey) {
          playFromStart();
        } else {
          togglePlayback();
        }
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [togglePlayback, playFromStart]);

  const chooseRate = (value: string) => {
    const next = Number(value);
    if (!PLAYBACK_RATES.includes(next as (typeof PLAYBACK_RATES)[number])) return;
    setRate(next);
    waveSurferRef.current?.setPlaybackRate(next, true);
  };

  const pausePlayback = () => waveSurferRef.current?.pause();

  const insertSilence = () => {
    pausePlayback();
    onAppendEdit({ op: "insert_silence", startSeconds: playhead, durationSeconds: 0.2 });
  };
  const deleteSelection = () => {
    pausePlayback();
    onAppendEdit({ op: "delete_range", startSeconds: selLo, endSeconds: selHi });
    setSelectionStart(0);
    setSelectionEnd(0);
  };
  const splitClip = () => {
    pausePlayback();
    onAppendEdit({ op: "split", startSeconds: hasSelection ? selHi : Number((duration / 2).toFixed(2)) });
  };
  const mergeClip = () => {
    pausePlayback();
    onAppendEdit({ op: "merge_next" });
  };

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
          <WaveformPane
            key={clip.id}
            audioUrl={clip.audioUrl}
            durationSeconds={duration}
            selectionStart={selectionStart}
            selectionEnd={selectionEnd}
            onSelectionChange={(s, e) => {
              setSelectionStart(s);
              setSelectionEnd(e);
            }}
            onCursorChange={setPlayhead}
            onHoverChange={setHover}
            onReady={(ws) => {
              waveSurferRef.current = ws;
              ws?.setPlaybackRate(rate, true);
            }}
            onAudioReady={(ws) => {
              ws.setPlaybackRate(rate, true);
              if (!autoplay) return;
              onAutoplayConsumed();
              ws.setTime(0);
              setPlayhead(0);
              void ws.play();
            }}
            onPlayingChange={setIsPlaying}
          />
        ) : (
          <div className="flex h-[150px] items-center justify-center bg-secondary/40 text-sm text-muted-foreground">
            No audio for this clip.
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1 border-t border-border px-3 py-2.5">
        <Button type="button" variant="ghost" size="icon" className="h-8 w-8" onClick={togglePlayback}>
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

        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={onUndo} disabled={!canUndo}>
          Undo
        </Button>
        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={onRedo} disabled={!canRedo}>
          Redo
        </Button>

        <Separator orientation="vertical" className="mx-1 h-5" />

        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={splitClip}>
          Split
        </Button>
        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={mergeClip}>
          Merge next
        </Button>
        <Button type="button" variant="ghost" size="sm" className="h-8" onClick={insertSilence}>
          Insert silence
        </Button>
        {hasSelection ? (
          <Button type="button" variant="ghost" size="sm" className="h-8" onClick={deleteSelection}>
            Delete selection
          </Button>
        ) : null}

        <div className="ml-auto flex items-center gap-3 pr-1 text-xs tabular-nums text-muted-foreground">
          <span className={cn(hasSelection && "text-foreground")}>
            {hasSelection
              ? `Sel ${formatClock(selLo)}–${formatClock(selHi)}`
              : hover !== null
                ? `Hover ${formatClock(hover)}`
                : "Sel none"}
          </span>
          <span className="text-foreground">
            {formatClock(playhead)} / {formatClock(duration)}
          </span>
        </div>
      </div>
    </section>
  );
}
