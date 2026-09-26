"use client";

import { cn } from "@midday/ui/cn";
import { Progress } from "@midday/ui/progress";
import { SubmitButton } from "@midday/ui/submit-button";
import { useToast } from "@midday/ui/use-toast";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import { type FileRejection, useDropzone } from "react-dropzone";
import { fetchDatasetRuns, fetchProjects } from "@/components/lab/speechcraft-api";
import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import {
  demoEnabled,
  FALLBACK_DEMO_PROJECT_ID,
  FALLBACK_DEMO_RUN_ID,
  withDemo,
} from "@/lib/demo";
import { createProject, makeProjectId, uploadRecording } from "./wizard/wizard-api";

type Phase = "idle" | "uploading" | "error";

export function IngestDropzone() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const demo = demoEnabled(searchParams);
  const { toast } = useToast();
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [current, setCurrent] = useState(0);
  const [total, setTotal] = useState(0);
  const [skipping, setSkipping] = useState(false);
  const startedRef = useRef(false);

  const continueWithExisting = useCallback(async () => {
    if (skipping) return;
    setSkipping(true);
    let projectId = FALLBACK_DEMO_PROJECT_ID;
    let runId: string | null = FALLBACK_DEMO_RUN_ID;
    try {
      const projects = await fetchProjects();
      const preferred =
        projects.find((project) => project.id === FALLBACK_DEMO_PROJECT_ID) ??
        projects.find((project) => project.id !== "phase1-demo") ??
        projects[0];
      if (preferred) {
        projectId = preferred.id;
        runId = projectId === FALLBACK_DEMO_PROJECT_ID ? FALLBACK_DEMO_RUN_ID : null;
        const runs = await fetchDatasetRuns(projectId);
        const reviewable =
          runs.find((run) => run.id === FALLBACK_DEMO_RUN_ID) ??
          runs.find((run) => (run.output_summary?.clip_count ?? 0) > 0) ??
          runs[0];
        if (reviewable) runId = reviewable.id;
      }
    } catch {
      // Backend offline — still advance with the handoff IDs.
    }

    const target = new URLSearchParams({ project: projectId });
    if (runId) target.set("run", runId);
    router.push(withDemo(`/login3?${target.toString()}`, true));
    setSkipping(false);
  }, [router, skipping]);

  const ingest = useCallback(
    async (files: File[]) => {
      if (startedRef.current || files.length === 0) return;
      startedRef.current = true;

      // Demo mode: skip the real upload and continue with an existing project.
      if (demo) {
        await continueWithExisting();
        return;
      }

      setPhase("uploading");
      setTotal(files.length);
      setCurrent(0);
      setProgress(0);

      const name =
        files.length === 1
          ? files[0]!.name.replace(/\.[a-z0-9]+$/i, "")
          : `${files.length} recordings`;
      const projectId = makeProjectId(files[0]!.name);

      try {
        await createProject(projectId, name);
        for (let i = 0; i < files.length; i++) {
          setCurrent(i + 1);
          await uploadRecording(projectId, files[i]!, (pct) => {
            // Aggregate progress across all files.
            setProgress(Math.round(((i + pct / 100) / files.length) * 100));
          });
        }
        setProgress(100);
        router.push(withDemo(`/login3?project=${encodeURIComponent(projectId)}`, demo));
      } catch (err) {
        startedRef.current = false;
        setPhase("error");
        toast({
          variant: "error",
          duration: 4000,
          title: "Upload failed",
          description:
            err instanceof SpeechcraftApiError ? err.detail : "Could not ingest the recordings.",
        });
      }
    },
    [router, toast, demo, continueWithExisting],
  );

  const onDropRejected = ([reject]: FileRejection[]) => {
    if (reject?.errors.find(({ code }) => code === "file-invalid-type")) {
      toast({ duration: 2500, variant: "error", title: "Only .wav files are supported." });
    }
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop: (files) => void ingest(files),
    onDropRejected,
    accept: { "audio/wav": [".wav"], "audio/x-wav": [".wav"] },
    disabled: phase === "uploading" || skipping,
  });

  if (phase === "uploading") {
    return (
      <>
        <div className="text-center space-y-2">
          <h1 className="text-lg lg:text-xl mb-4 font-serif">Ingesting your dataset</h1>
          <p className="font-sans text-sm text-[#878787]">
            Uploading recording {current} of {total}…
          </p>
        </div>
        <div className="mt-10 lg:mt-12 space-y-3">
          <Progress value={progress} />
          <div className="flex items-center justify-between text-xs text-[#878787]">
            <span>{progress}%</span>
            <span>{progress >= 100 ? "Finishing…" : "Streaming to backend"}</span>
          </div>
        </div>
      </>
    );
  }

  return (
    <div {...getRootProps({ onClick: (evt) => evt.stopPropagation() })} className="relative">
      <div
        className={cn(
          "absolute inset-0 -m-8 lg:-m-12 z-10 flex items-center justify-center bg-background transition-opacity",
          isDragActive ? "opacity-100" : "opacity-0 pointer-events-none",
        )}
      >
        <p className="text-sm text-[#878787]">Drop your .wav files here.</p>
      </div>

      <input {...getInputProps()} id="upload-files" />

      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Ingest your dataset</h1>
        <p className="font-sans text-sm text-[#878787]">
          {phase === "error"
            ? "Something went wrong. Drag & drop your .wav files to try again."
            : demo
              ? "Demo mode — continue with the current project, or upload new .wav recordings."
              : "Drag & drop or upload your .wav recordings."}
        </p>
      </div>

      <div className="mt-10 lg:mt-12 space-y-3">
        {demo ? (
          <SubmitButton
            type="button"
            onClick={() => void continueWithExisting()}
            className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
            isSubmitting={skipping}
          >
            Continue with current project
          </SubmitButton>
        ) : null}
        <SubmitButton
          type="button"
          onClick={() => document.getElementById("upload-files")?.click()}
          className={
            demo
              ? "bg-transparent border border-border px-6 py-4 text-foreground font-medium flex space-x-2 h-[40px] w-full"
              : "bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
          }
          isSubmitting={false}
        >
          Upload
        </SubmitButton>
      </div>
    </div>
  );
}
