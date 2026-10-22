"use client";

import { SubmitButton } from "@midday/ui/submit-button";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { demoEnabled, FALLBACK_DEMO_PROJECT_ID, FALLBACK_DEMO_RUN_ID, withDemo } from "@/lib/demo";
import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import { createDatasetRun, listProjectSourceRecordings, resolveWhisperModel, startRun } from "@/components/wizard/wizard-api";

type SpeakerModeChoice = "single" | "multiple";

function attemptFor(projectId: string, mode: SpeakerModeChoice): string {
  const key = `speechcraft-onboarding-attempt:${projectId}:${mode}`;
  try {
    let attempt = localStorage.getItem(key);
    if (!attempt) {
      attempt = `onboarding:${projectId}:${crypto.randomUUID()}`;
      localStorage.setItem(key, attempt);
    }
    return attempt;
  } catch {
    return `onboarding:${projectId}:${mode}:${crypto.randomUUID()}`;
  }
}

export function SpeakerMode() {
  const router = useRouter();
  const params = useSearchParams();
  const projectId = params.get("project");
  const demo = demoEnabled(params);
  const [choice, setChoice] = useState<SpeakerModeChoice | null>(null);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [checkAttempt, setCheckAttempt] = useState(0);
  const submitted = useRef(false);

  useEffect(() => {
    if (!projectId) return;
    try {
      const saved = sessionStorage.getItem(`speechcraft-speaker-mode:${projectId}`);
      if (saved === "single" || saved === "multiple") setChoice(saved);
    } catch { /* selection remains available for this page */ }
  }, [projectId]);

  useEffect(() => {
    if (!projectId) {
      setBlocked("No project was provided. Return to import to upload recordings.");
      setChecking(false);
      return;
    }
    if (demo) {
      setBlocked(null);
      setChecking(false);
      return;
    }
    let active = true;
    setChecking(true);
    listProjectSourceRecordings(projectId).then((recordings) => {
      if (!active) return;
      setBlocked(recordings.length === 0 ? "This project has no imported recordings yet. Return to import and add audio." : null);
    }).catch((error: unknown) => {
      if (active) setBlocked(error instanceof SpeechcraftApiError ? error.detail : "Could not verify the imported recordings.");
    }).finally(() => { if (active) setChecking(false); });
    return () => { active = false; };
  }, [projectId, demo, checkAttempt]);

  const continueFlow = async () => {
    if (!choice || busy || submitted.current || blocked) return;
    submitted.current = true;
    setBusy(true);
    setSubmitError(null);
    try {
      if (!projectId) throw new Error("Project is missing. Return to import and try again.");
      if (demo) {
        const target = choice === "single" ? "/onboarding/processing" : "/onboarding/choose-speaker";
        router.push(withDemo(`${target}?project=${encodeURIComponent(FALLBACK_DEMO_PROJECT_ID)}&run=${encodeURIComponent(FALLBACK_DEMO_RUN_ID)}`, true));
        return;
      }
      const [recordings, resolved] = await Promise.all([listProjectSourceRecordings(projectId), resolveWhisperModel()]);
      if (recordings.length === 0) throw new Error("No recordings are available. Return to import and upload audio.");
      if (!resolved.model) throw new Error(resolved.error || "The speech model is not ready. Return to setup.");
      const singleSpeaker = choice === "single";
      const run = await createDatasetRun(projectId, {
        source_recording_ids: recordings.map((recording) => recording.id),
        wizard_attempt: attemptFor(projectId, choice),
        whisper_model_size: resolved.model,
        language: "auto",
        single_speaker: singleSpeaker,
      });
      if (run.status === "pending") await startRun(run.id);
      const target = singleSpeaker ? "/onboarding/processing" : "/onboarding/choose-speaker";
      router.push(withDemo(`${target}?project=${encodeURIComponent(projectId)}&run=${encodeURIComponent(run.id)}`, demo));
    } catch (error) {
      submitted.current = false;
      setBusy(false);
      setSubmitError(error instanceof SpeechcraftApiError ? error.detail : error instanceof Error ? error.message : "Could not start processing.");
    }
  };

  const goImport = () => router.push(withDemo(projectId ? `/onboarding/import?project=${encodeURIComponent(projectId)}` : "/onboarding/import", demo));

  return (
    <div>
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">How many speakers are in your recordings?</h1>
        <p className="font-sans text-sm text-[#878787]">We&apos;ll use this to process your audio correctly.</p>
      </div>

      <div className="mt-8 space-y-3" role="radiogroup" aria-label="Number of speakers">
        {([
          ["single", "One speaker", "The same person speaks throughout the recordings."],
          ["multiple", "Multiple speakers / Not sure", "Automatically identify different voices."],
        ] as const).map(([value, title, description]) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={choice === value}
            disabled={busy || checking || Boolean(blocked)}
            onClick={() => {
              setChoice(value);
              if (projectId) {
                try { sessionStorage.setItem(`speechcraft-speaker-mode:${projectId}`, value); } catch { /* page state remains available */ }
              }
            }}
            className={`w-full border px-4 py-3 text-left transition-colors ${choice === value ? "border-primary bg-primary/5" : "border-border hover:border-foreground/40"}`}
          >
            <span className="flex items-start gap-3">
              <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border ${choice === value ? "border-primary" : "border-muted-foreground"}`}>
                {choice === value ? <span className="h-2 w-2 rounded-full bg-primary" /> : null}
              </span>
              <span>
                <span className="block text-sm font-medium">{title}</span>
                <span className="mt-1 block text-xs text-[#878787]">{description}</span>
              </span>
            </span>
          </button>
        ))}
      </div>

      {blocked ? <p role="alert" className="mt-4 text-center text-sm text-destructive">{blocked}</p> : null}
      {submitError ? <p role="alert" className="mt-4 text-center text-sm text-destructive">{submitError}</p> : null}
      {checking ? <p className="mt-4 text-center text-xs text-[#878787]">Checking imported recordings…</p> : null}
      {blocked && projectId ? (
        <button type="button" onClick={() => setCheckAttempt((attempt) => attempt + 1)} className="mt-3 w-full text-sm underline underline-offset-4">Try again</button>
      ) : null}

      <div className="mt-8 space-y-3">
        <SubmitButton
          type="button"
          onClick={() => void continueFlow()}
          disabled={!choice || busy || checking || Boolean(blocked)}
          className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full disabled:opacity-50"
          isSubmitting={busy}
        >
          Continue
        </SubmitButton>
        <button type="button" onClick={goImport} className="w-full py-2 text-sm text-[#878787] hover:text-foreground">Back to import</button>
      </div>
    </div>
  );
}
