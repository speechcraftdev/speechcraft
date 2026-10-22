"use client";

import { cn } from "@midday/ui/cn";
import { Progress } from "@midday/ui/progress";
import { SubmitButton } from "@midday/ui/submit-button";
import { useToast } from "@midday/ui/use-toast";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { type FileRejection, useDropzone } from "react-dropzone";
import { fetchDatasetRuns, fetchProjects } from "@/components/lab/speechcraft-api";
import { SpeechcraftApiError } from "@/components/lab/speechcraft-write-api";
import {
  demoEnabled,
  FALLBACK_DEMO_PROJECT_ID,
  FALLBACK_DEMO_RUN_ID,
  withDemo,
} from "@/lib/demo";
import { resolveImportTarget } from "./wizard/import-project.mjs";
import { createProject, fetchProject, makeProjectId, uploadRecording } from "./wizard/wizard-api";

type Phase = "idle" | "uploading" | "error";

function datasetName(files: File[]): string {
  return files.length === 1
    ? files[0]!.name.replace(/\.[a-z0-9]+$/i, "")
    : `${files.length} recordings`;
}

export function IngestDropzone() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const demo = demoEnabled(searchParams);
  const requestedProjectId = searchParams.get("project")?.trim() || "";
  const { toast } = useToast();
  const [phase, setPhase] = useState<Phase>("idle");
  const [progress, setProgress] = useState(0);
  const [current, setCurrent] = useState(0);
  const [total, setTotal] = useState(0);
  const [skipping, setSkipping] = useState(false);
  const [projectName, setProjectName] = useState<string | null>(null);
  const [projectMissing, setProjectMissing] = useState(false);
  const [projectCheckError, setProjectCheckError] = useState<string | null>(null);
  const [checkingProject, setCheckingProject] = useState(Boolean(requestedProjectId) && !demo);
  const startedRef = useRef(false);

  useEffect(() => {
    if (demo || !requestedProjectId) {
      setProjectName(null);
      setProjectMissing(false);
      setCheckingProject(false);
      return;
    }
    let active = true;
    setCheckingProject(true);
    fetchProject(requestedProjectId).then((project) => {
      if (!active) return;
      setProjectName(project.name);
      setProjectMissing(false);
      setProjectCheckError(null);
    }).catch((error: unknown) => {
      if (!active) return;
      setProjectName(null);
      const missing = error instanceof SpeechcraftApiError && error.status === 404;
      setProjectMissing(missing);
      setProjectCheckError(missing ? null : error instanceof SpeechcraftApiError ? error.detail : "The project could not be verified.");
    }).finally(() => {
      if (active) setCheckingProject(false);
    });
    return () => { active = false; };
  }, [demo, requestedProjectId]);

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
    router.push(withDemo(`/onboarding/speakers?${target.toString()}`, true));
    setSkipping(false);
  }, [router, skipping]);

  const ingest = useCallback(
    async (files: File[]) => {
      if (startedRef.current || files.length === 0 || projectMissing || checkingProject || projectCheckError) return;
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

      try {
        let projectId: string;
        if (requestedProjectId) {
          const project = await fetchProject(requestedProjectId).catch((error: unknown) => {
            if (error instanceof SpeechcraftApiError && error.status === 404) return null;
            throw error;
          });
          const target = resolveImportTarget({ requestedProjectId, project });
          if (target.action === "reject") {
            startedRef.current = false;
            setPhase("idle");
            setProjectMissing(true);
            return;
          }
          projectId = target.projectId;
        } else {
          projectId = makeProjectId(files[0]!.name);
          const name = datasetName(files);
          try {
            await createProject(projectId, name);
          } catch (error) {
            if (error instanceof SpeechcraftApiError && error.detail.includes("Project name already exists")) {
              await createProject(projectId, `${name} ${projectId.slice(-6)}`);
            } else {
              throw error;
            }
          }
        }
        for (let i = 0; i < files.length; i++) {
          setCurrent(i + 1);
          await uploadRecording(projectId, files[i]!, (pct) => {
            setProgress(Math.round(((i + pct / 100) / files.length) * 100));
          });
        }
        setProgress(100);
        router.push(withDemo(`/onboarding/speakers?project=${encodeURIComponent(projectId)}`, demo));
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
    [router, toast, demo, continueWithExisting, requestedProjectId, projectMissing, checkingProject, projectCheckError],
  );

  const onDropRejected = ([reject]: FileRejection[]) => {
    if (reject?.errors.find(({ code }) => code === "file-invalid-type")) {
      toast({ duration: 2500, variant: "error", title: "Choose WAV, MP3, FLAC, M4A, AAC, OGG, Opus, or AIFF audio." });
    }
  };

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop: (files) => void ingest(files),
    onDropRejected,
    accept: {
      "audio/*": [".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"],
    },
    disabled: phase === "uploading" || skipping || projectMissing || checkingProject || Boolean(projectCheckError),
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
            <span>{progress >= 99 ? "Validating audio…" : "Verified transfer"}</span>
          </div>
        </div>
      </>
    );
  }

  if (projectMissing) {
    return (
      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">This project is not available</h1>
        <p role="alert" className="font-sans text-sm text-[#878787]">
          The import link does not match a project on this Speechcraft installation. No new project was created.
        </p>
        <div className="mt-10">
          <SubmitButton
            type="button"
            onClick={() => router.push(withDemo("/onboarding/import", demo))}
            className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
            isSubmitting={false}
          >
            Start a new import
          </SubmitButton>
        </div>
      </div>
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
        <p className="text-sm text-[#878787]">Drop supported audio files here.</p>
      </div>

      <input {...getInputProps()} id="upload-files" />

      <div className="text-center space-y-2">
        <h1 className="text-lg lg:text-xl mb-4 font-serif">Ingest your dataset</h1>
        <p className="font-sans text-sm text-[#878787]">
          {phase === "error"
            ? "Something went wrong. Drag & drop supported audio files to try again."
              : projectCheckError
              ? projectCheckError
            : checkingProject
              ? "Checking this project…"
            : demo
              ? "Demo mode — continue with the current project, or upload new audio recordings."
              : requestedProjectId
                ? `Adding recordings to ${projectName || "this project"}. Existing recordings stay in place.`
                : "Drag & drop or upload WAV, MP3, FLAC, M4A, AAC, OGG, Opus, or AIFF recordings."}
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
          disabled={checkingProject || Boolean(projectCheckError)}
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
