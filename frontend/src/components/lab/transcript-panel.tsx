"use client";

import { Card } from "@midday/ui/card";
import { Textarea } from "@midday/ui/textarea";
import { useEffect, useRef, useState } from "react";
import { SavingBar } from "@/components/saving-bar";
import { type LabClip, formatQcScore } from "./lab-data";
import { formatNullableQcScore } from "./qc-score-display.mjs";

type TranscriptPanelProps = {
  clip: LabClip;
  onTranscriptChange: (text: string) => void;
};

export function TranscriptPanel({
  clip,
  onTranscriptChange,
}: TranscriptPanelProps) {
  const [draft, setDraft] = useState(clip.transcript);
  const [isPending, setIsPending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset the draft when the active clip changes.
  useEffect(() => {
    setDraft(clip.transcript);
    setIsPending(false);
    if (debounceRef.current) clearTimeout(debounceRef.current);
  }, [clip.id, clip.transcript]);

  // Auto-grow the textarea to fit content.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [draft]);

  const handleTranscript = (value: string) => {
    setDraft(value);
    setIsPending(true);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      onTranscriptChange(value);
      setIsPending(false);
    }, 600);
  };

  return (
    <section className="flex flex-col border border-border">
      <div className="flex h-11 items-center border-b border-border px-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Transcript
        </span>
      </div>

      <div className="relative flex flex-col gap-4 p-4">
        <Textarea
          ref={textareaRef}
          value={draft}
          onChange={(event) => handleTranscript(event.target.value)}
          rows={1}
          placeholder="Transcript text"
          className="resize-none border-border text-sm leading-relaxed"
        />

        <div className="grid grid-cols-1 gap-3">
          <Card className="border-border p-4">
            <p className="mb-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              QC scores
            </p>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  Transcript confidence
                </p>
                <p className="mt-1 text-2xl font-medium tabular-nums">
                  {formatNullableQcScore(clip.transcriptMatchRaw, formatQcScore)}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-muted-foreground">
                  Speaker Purity · uncalibrated review score
                </p>
                <p className="mt-1 text-2xl font-medium tabular-nums">
                  {formatNullableQcScore(clip.speakerCheckRaw, formatQcScore)}
                </p>
                <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
                  Similarity score, not a probability. Listen before accepting this clip.
                </p>
              </div>
            </div>
          </Card>

        </div>

        <SavingBar isPending={isPending} />
      </div>
    </section>
  );
}
