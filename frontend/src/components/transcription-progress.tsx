"use client";

import { Progress } from "@midday/ui/progress";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DiagnosticsSheet,
  type DiagnosticStage,
} from "@/components/lab/diagnostics-drawer";
import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import { demoEnabled, withDemo } from "@/lib/demo";
import {
  type DatasetRunView,
  createDatasetRun,
  generateQc,
  refreshRun,
  resolveWhisperModel,
  resumeProcessing,
  startRun,
  waitForRun,
} from "./wizard/wizard-api";
import {
  completedAtStage,
  WIZARD_SELECTED_SPEAKER_STOP_AFTER,
} from "./wizard/processing-plan.mjs";

// The worker's supported sequence is slicing, then transcript and speaker QC.
type Phase = "slicing" | "scoring" | "error";

const PHASE_COPY: Record<Exclude<Phase, "error">, { title: string; sub: string; pct: number }> = {
  slicing: { title: "Slicing clips", sub: "Running the locked VR O0_4 slicing pass…", pct: 55 },
  scoring: { title: "Scoring transcript and speaker quality", sub: "Running Transcript Correctness and Speaker Purity…", pct: 90 },
};

function detectLanguage(run: DatasetRunView | null): string | null {
  if (!run) return null;
  const out = run.output_summary as Record<string, unknown>;
  const raw =
    (out.language as string) ??
    (out.detected_language as string) ??
    ((out.asr as Record<string, unknown> | undefined)?.language as string);
  return typeof raw === "string" && raw ? raw : null;
}

export function TranscriptionProgress() {
  const router = useRouter();
  const params = useSearchParams();
  const projectId = params.get("project");
  const runId = params.get("run");
  const demo = demoEnabled(params);

  const [phase, setPhase] = useState<Phase>("slicing");
  const [error, setError] = useState<string | null>(null);
  const [run, setRun] = useState<DatasetRunView | null>(null);
  const [language, setLanguage] = useState<string | null>(null);
  const startedRef = useRef(false);

  const stages: DiagnosticStage[] = [
    { key: "diarization", label: "Speaker detection", status: "completed" },
    { key: "slice", label: "VR O0_4 slicing", status: phase === "slicing" ? "running" : phase === "scoring" ? "completed" : "failed" },
    { key: "asr", label: "Transcript Correctness", status: phase === "slicing" ? "pending" : phase === "scoring" ? "running" : "failed" },
    { key: "qc", label: "Speaker Purity", status: phase === "scoring" ? "running" : "failed" },
  ];

  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    if (!projectId || !runId) {
      setPhase("error");
      setError("Missing project/run in the URL. Restart from ingest.");
      return;
    }
    if (demo) {
      setLanguage("en");
      const timers = [
        setTimeout(() => setPhase("slicing"), 1500),
        setTimeout(() => setPhase("scoring"), 3000),
        setTimeout(
          () =>
            router.push(
              withDemo(
                `/lab?project=${encodeURIComponent(projectId)}&run=${encodeURIComponent(runId)}`,
                true,
              ),
            ),
          4500,
        ),
      ];
      return () => timers.forEach(clearTimeout);
    }
    const ctrl = new AbortController();
    (async () => {
      try {
        setPhase("slicing");
        await resumeProcessing(runId, WIZARD_SELECTED_SPEAKER_STOP_AFTER);
        const sliced = await waitForRun(
          runId,
          (r) => completedAtStage(r, "candidate_review_clips"),
          { signal: ctrl.signal, onTick: setRun },
        );
        setLanguage(detectLanguage(sliced));

        setPhase("scoring");
        await generateQc(runId, false);
        await waitForRun(
          runId,
          (r) => completedAtStage(r, "speaker_purity"),
          { signal: ctrl.signal, onTick: setRun },
        );

        router.push(
          withDemo(
            `/lab?project=${encodeURIComponent(projectId)}&run=${encodeURIComponent(runId)}`,
            demo,
          ),
        );
      } catch (err) {
        if (ctrl.signal.aborted) return;
        setPhase("error");
        setError(err instanceof SpeechcraftApiError ? err.detail : "Processing failed.");
      }
    })();
    return () => ctrl.abort();
  }, [projectId, runId, router, demo]);

  // Detected language is externally-known & catastrophic if wrong -> a
  // correctable value at the moment of detection. Changing it = a fresh run.
  const overrideLanguage = useCallback(
    async (lang: string) => {
      if (demo) {
        setLanguage(lang);
        return;
      }
      if (!projectId) return;
      try {
        const resolved = await resolveWhisperModel();
        if (resolved.model === null) throw new SpeechcraftApiError(0, resolved.error);
        const fresh = await createDatasetRun(projectId, {
          whisper_model_size: resolved.model,
          language: lang,
        });
        await startRun(fresh.id);
        router.push(
          `/login4?project=${encodeURIComponent(projectId)}&run=${encodeURIComponent(fresh.id)}`,
        );
      } catch (err) {
        setPhase("error");
        setError(err instanceof SpeechcraftApiError ? err.detail : "Could not re-run with that language.");
      }
    },
    [projectId, router, demo],
  );

  if (phase === "error") {
    return (
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Processing failed</h1>
        <p className="font-sans text-sm text-[#878787]">{error}</p>
        <div className="pt-6">
          <DiagnosticsSheet runId={runId} runStatus={run?.status ?? "failed"} runStage={run?.stage} stages={stages} />
        </div>
      </div>
    );
  }

  const copy = PHASE_COPY[phase];

  return (
    <>
      <div className="text-center space-y-2">
        {demo ? (
          <span className="mb-2 inline-block border border-amber-500/50 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-500">
            Demo — simulated, no processing running
          </span>
        ) : null}
        <h1 className="text-lg lg:text-xl mb-4 font-serif">{copy.title}</h1>
        <p className="font-sans text-sm text-[#878787]">
          {demo ? "Simulated progress for UI review — nothing is really running." : copy.sub}
        </p>
      </div>

      <div className="mt-10 lg:mt-12 space-y-3">
        <Progress value={copy.pct} />
        <div className="flex items-center justify-between text-xs text-[#878787]">
          <span>{demo ? "simulating" : run?.stage ? run.stage.replace(/_/g, " ") : "working"}</span>
          <span>{demo ? "Demo mode" : "This can take a while for large datasets."}</span>
        </div>
      </div>

      {language ? (
        <div className="mt-6 flex items-center justify-center gap-2 text-sm">
          <span className="text-[#878787]">Detected language:</span>
          <select
            value={language}
            onChange={(e) => void overrideLanguage(e.target.value)}
            className="border border-border bg-transparent px-2 py-1 text-sm"
            aria-label="Detected language"
          >
            {["en", "es", "fr", "de", "it", "pt"].map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
            {!["en", "es", "fr", "de", "it", "pt"].includes(language) ? (
              <option value={language}>{language}</option>
            ) : null}
          </select>
          <span className="text-[10px] text-muted-foreground">(changing re-runs)</span>
        </div>
      ) : null}

      <div className="mt-8 flex justify-center">
        <DiagnosticsSheet runId={runId} runStatus={run?.status} runStage={run?.stage} stages={stages} />
      </div>
    </>
  );
}
