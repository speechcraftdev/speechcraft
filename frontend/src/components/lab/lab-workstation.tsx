"use client";

import { cn } from "@midday/ui/cn";
import { Button } from "@midday/ui/button";
import { Icons } from "@midday/ui/icons";
import { useToast } from "@midday/ui/use-toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { AudioEditMutex } from "./clip-lab-editor/audio-edit-mutex";
import { ClipLabPanel } from "./clip-lab-panel";
import { ClipQueue } from "./clip-queue";
import { DatasetHealthPage } from "./dataset-health-page";
import { DiagnosticsSheet } from "./diagnostics-drawer";
import { ExportDialog } from "./export-dialog";
import { InspectorRail } from "./inspector-rail";
import { KeyboardBar } from "./keyboard-bar";
import { ProjectPicker } from "./project-picker";
import { LabRerunDialog } from "./rerun-dialog";
import { TranscriptPanel } from "./transcript-panel";
import type { HumanLabeledClip } from "./qc-logic";
import {
  type ClipEdit,
  type LabClip,
  type MachineBucket,
  type ReviewStatus,
  type SortMode,
  createMockClips,
  filterClips,
  sortClips,
  STATUS_LABELS,
} from "./lab-data";
import { demoEnabled } from "@/lib/demo";
import {
  fetchClipLabView,
  fetchDatasetRuns,
  fetchProjects,
  mapApiClip,
  pickReviewableRun,
  type QcSubset,
} from "./speechcraft-api";
import {
  type DatasetAudioEditOperation,
  type DatasetClipLabClipView,
  clearQcSubset,
  setQcSubset,
  SpeechcraftApiError,
  appendAudioOperation,
  markReferenceClipCandidate,
  mergeClipLabWriteResponse,
  patchClipLab,
  redoAudioOperation,
  undoAudioOperation,
} from "./speechcraft-write-api";

const PRESET_TAGS = [
  "mispronunciation",
  "background noise",
  "clipping",
  "overlap",
  "filler word",
  "breath",
];

type Mode = "qc" | "lab";

// Isolated experiment switch: set false to remove the human-label overlays.
const SHOW_HUMAN_LABEL_OVERLAY = true;

/** Optimistic-concurrency tokens the backend requires on every clip write. */
function tokensFor(clip: LabClip) {
  return {
    expected_manifest_sha256: clip.manifestSha as string,
    expected_clip_version: clip.clipVersion as number,
  };
}

const isLiveBacked = (clip: LabClip) =>
  clip.manifestSha != null && clip.clipVersion != null;

function passesQcSubset(clip: LabClip, subset: QcSubset | null): boolean {
  if (!subset) return true;
  if (subset.transcript_match_min === 0 && subset.speaker_check_min === 0) return true;
  return (
    clip.transcriptMatchRaw != null &&
    clip.speakerCheckRaw != null &&
    clip.transcriptMatchRaw >= subset.transcript_match_min &&
    clip.speakerCheckRaw >= subset.speaker_check_min
  );
}

export function LabWorkstation() {
  const searchParams = useSearchParams();
  const { toast, update } = useToast();
  const queryClient = useQueryClient();
  const demo = demoEnabled(searchParams);
  const paramProjectId = searchParams.get("project");
  const paramRunId = searchParams.get("run");
  // Demo URLs with real handoff IDs use live data; only the default demo route
  // (or its legacy demo-review/demo IDs) should seed the mock clips.
  const useMockClips =
    demo &&
    (!paramProjectId || paramProjectId === "demo-review") &&
    (!paramRunId || paramRunId === "demo");
  const audioEditMutexRef = useRef(new AudioEditMutex());
  const [audioEditInFlight, setAudioEditInFlight] = useState(false);

  const tryBeginAudioEdit = useCallback(() => {
    const started = audioEditMutexRef.current.tryBegin();
    if (started) setAudioEditInFlight(true);
    return started;
  }, []);

  const endAudioEdit = useCallback(() => {
    audioEditMutexRef.current.end();
    setAudioEditInFlight(false);
  }, []);

  const runAudioEdit = useCallback(
    async (fn: () => Promise<boolean>): Promise<boolean> => {
      if (!tryBeginAudioEdit()) return false;
      try {
        return await fn();
      } finally {
        endAudioEdit();
      }
    },
    [tryBeginAudioEdit, endAudioEdit],
  );

  // ── Real data chain: projects → dataset runs → clip-lab view ──
  const { data: projects = [] } = useQuery({
    queryKey: ["sc-projects"],
    queryFn: fetchProjects,
    staleTime: 60_000,
    enabled: !useMockClips,
  });

  const activeProject =
    projects.find((p) => p.id === paramProjectId) ??
    (paramProjectId && !useMockClips
      ? { id: paramProjectId, name: paramProjectId }
      : null) ??
    projects[0] ??
    null;
  const projectId = activeProject?.id ?? null;

  const { data: runs = [], isLoading: runsLoading } = useQuery({
    queryKey: ["sc-runs", projectId],
    queryFn: () => fetchDatasetRuns(projectId!),
    enabled: !useMockClips && !!projectId,
    staleTime: 60_000,
  });

  const run = useMemo(() => {
    if (paramRunId && paramRunId !== "demo") {
      return (
        runs.find((candidate) => candidate.id === paramRunId) ?? {
          id: paramRunId,
          project_id: projectId ?? "",
          stage: "",
          status: "",
        }
      );
    }
    return pickReviewableRun(runs);
  }, [runs, paramRunId, projectId]);
  const runId = run?.id ?? null;

  const {
    data: view,
    isLoading: viewLoading,
    error: viewError,
  } = useQuery({
    queryKey: ["sc-cliplab", runId],
    queryFn: () => fetchClipLabView(runId!),
    enabled: !useMockClips && !!runId,
    staleTime: Number.POSITIVE_INFINITY,
  });

  // Committed (or default) QC thresholds — drives machineBucket below so
  // "auto_kept" in Clip Lab means whatever Dataset Health has committed,
  // not a hardcoded guess. Refetched whenever Dataset Health commits.
  // ── Local (optimistic) working copy, seeded per run ──
  const [mode, setMode] = useState<Mode>("lab");
  const [exportOpen, setExportOpen] = useState(false);
  const [clips, setClips] = useState<LabClip[]>([]);
  const [activeClipId, setActiveClipId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [filterStatuses, setFilterStatuses] = useState<ReviewStatus[]>([]);
  const [filterTags, setFilterTags] = useState<string[]>([]);
  const [sortMode, setSortMode] = useState<SortMode>("source");
  const [filterBuckets, setFilterBuckets] = useState<MachineBucket[]>([]);
  const [autoplayClipId, setAutoplayClipId] = useState<string | null>(null);
  const [activeQcSubset, setActiveQcSubset] = useState<QcSubset | null>(null);
  const [draftQcThresholds, setDraftQcThresholds] = useState<{
    transcript_match_min: number;
    speaker_check_min: number;
  }>({ transcript_match_min: 0, speaker_check_min: 0 });
  const seededRunRef = useRef<string | null>(null);

  useEffect(() => {
    if (!useMockClips && view && runId && seededRunRef.current !== runId) {
      const mapped = view.clips.map((clip, index) =>
        mapApiClip(clip, index, runId, view.candidate_manifest_sha256, {
          transcriptMatchMin: view.active_qc_subset?.transcript_match_min ?? 0,
          speakerCheckMin: view.active_qc_subset?.speaker_check_min ?? 0,
        }),
      );
      setClips(mapped);
      setActiveClipId(mapped[0]?.id ?? null);
      setSearch("");
      setFilterStatuses([]);
      setFilterTags([]);
      setFilterBuckets([]);
      setActiveQcSubset(view.active_qc_subset);
      setDraftQcThresholds({
        transcript_match_min: view.active_qc_subset?.transcript_match_min ?? 0,
        speaker_check_min: view.active_qc_subset?.speaker_check_min ?? 0,
      });
      seededRunRef.current = runId;
    }
    // activeQcSubset intentionally excluded: this effect only seeds once per
    // run. Threshold changes after seeding are handled by the re-derive
    // effect below so in-progress edits aren't clobbered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, runId, useMockClips]);

  // Active subset thresholds can change after clips are already seeded. Re-derive
  // machineBucket/qcScore in place —
  // never touch review status, transcript, or audio edit state.
  useEffect(() => {
    if (useMockClips) return;
    const thresholds = {
      transcriptMatchMin: activeQcSubset?.transcript_match_min ?? 0,
      speakerCheckMin: activeQcSubset?.speaker_check_min ?? 0,
    };
    setClips((prev) =>
      prev.map((clip) => {
        const scoresKnown = clip.transcriptMatchRaw != null && clip.speakerCheckRaw != null;
        const hardFailed = clip.reasonCodes.length > 0;
        const passesGate =
          scoresKnown &&
          (clip.transcriptMatchRaw as number) >= thresholds.transcriptMatchMin &&
          (clip.speakerCheckRaw as number) >= thresholds.speakerCheckMin;
        const machineBucket: MachineBucket = !scoresKnown
          ? "needs_review"
          : hardFailed || !passesGate
            ? "auto_rejected"
            : "auto_kept";
        const qcScore = scoresKnown
          ? Math.max(
              0,
              Math.min(
                1,
                Math.min(
                  thresholds.transcriptMatchMin > 0 ? (clip.transcriptMatchRaw as number) / thresholds.transcriptMatchMin : 1,
                  thresholds.speakerCheckMin > 0 ? (clip.speakerCheckRaw as number) / thresholds.speakerCheckMin : 1,
                ),
              ),
            )
          : 0;
        if (clip.machineBucket === machineBucket && clip.qcScore === qcScore) return clip;
        return { ...clip, machineBucket, qcScore: Number(qcScore.toFixed(3)) };
      }),
    );
  }, [activeQcSubset, useMockClips]);

  // Demo / UI-review mode: seed from mock clips, no backend. Writes stay local
  // (mock clips have no manifestSha/clipVersion, so applyClipWrite is a no-op
  // beyond the optimistic update).
  useEffect(() => {
    if (useMockClips && seededRunRef.current !== "demo") {
      const mock = createMockClips();
      setClips(mock);
      setActiveClipId(mock[0]?.id ?? null);
      setActiveQcSubset(null);
      setDraftQcThresholds({ transcript_match_min: 0, speaker_check_min: 0 });
      seededRunRef.current = "demo";
    }
  }, [useMockClips]);

  const qcScopedClips = useMemo(
    () => clips.filter((clip) => passesQcSubset(clip, activeQcSubset)),
    [clips, activeQcSubset],
  );

  const visibleClips = useMemo(
    () => sortClips(filterClips(qcScopedClips, search, filterStatuses, filterTags, filterBuckets), sortMode),
    [qcScopedClips, search, filterStatuses, filterTags, filterBuckets, sortMode],
  );

  // If filters/search/sort hide the active clip, clear selection so the queue
  // and main panel never disagree about what's being edited.
  useEffect(() => {
    if (!activeClipId) return;
    if (visibleClips.some((clip) => clip.id === activeClipId)) return;
    setActiveClipId(null);
  }, [visibleClips, activeClipId]);

  const activeClip = useMemo(
    () => clips.find((c) => c.id === activeClipId) ?? null,
    [clips, activeClipId],
  );

  const availableTags = useMemo(() => {
    const set = new Set<string>();
    for (const clip of clips) for (const tag of clip.tags) set.add(tag);
    return Array.from(set).sort();
  }, [clips]);

  const allTags = useMemo(
    () => Array.from(new Set([...PRESET_TAGS, ...availableTags])),
    [availableTags],
  );

  const stats = useMemo(() => {
    const counts: Record<ReviewStatus, number> = {
      unresolved: 0,
      accepted: 0,
      rejected: 0,
    };
    for (const clip of qcScopedClips) counts[clip.status] += 1;
    const rows = (['unresolved', 'accepted', 'rejected'] as ReviewStatus[]).map((status) => {
      const matching = qcScopedClips.filter((clip) => clip.status === status);
      return {
        label: STATUS_LABELS[status],
        clips: matching.length,
        durationSeconds: matching.reduce((sum, clip) => sum + (clip.durationSeconds || 0), 0),
      };
    });
    const customTags = Array.from(
      new Set(qcScopedClips.flatMap((clip) => clip.tags.filter((tag) => !PRESET_TAGS.includes(tag)))),
    ).sort();
    const reviewedClipCount = qcScopedClips.filter(
      (clip) =>
        clip.status !== "unresolved" ||
        clip.tags.some((tag) => !PRESET_TAGS.includes(tag)),
    ).length;
    for (const tag of customTags) {
      const matching = qcScopedClips.filter((clip) => clip.tags.includes(tag));
      rows.push({
        label: tag,
        clips: matching.length,
        durationSeconds: matching.reduce((sum, clip) => sum + (clip.durationSeconds || 0), 0),
      });
    }
    const acceptedDurationSeconds = qcScopedClips
      .filter((clip) => clip.status === "accepted")
      .reduce((sum, clip) => sum + (clip.durationSeconds || 0), 0);
    const rejectedDurationSeconds = qcScopedClips
      .filter((clip) => clip.status === "rejected")
      .reduce((sum, clip) => sum + (clip.durationSeconds || 0), 0);
    const totalDurationSeconds = qcScopedClips.reduce(
      (sum, clip) => sum + (clip.durationSeconds || 0),
      0,
    );
    const reviewedDurationSeconds = acceptedDurationSeconds + rejectedDurationSeconds;
    const durations = qcScopedClips.map((clip) => clip.durationSeconds || 0).sort((a, b) => a - b);
    const meanDurationSeconds = durations.length
      ? durations.reduce((sum, duration) => sum + duration, 0) / durations.length
      : null;
    const medianDurationSeconds = durations.length
      ? durations.length % 2
        ? durations[Math.floor(durations.length / 2)]
        : (durations[durations.length / 2 - 1] + durations[durations.length / 2]) / 2
      : null;
    const variance = meanDurationSeconds === null
      ? null
      : durations.reduce((sum, duration) => sum + (duration - meanDurationSeconds) ** 2, 0) / durations.length;
    const statusReviewedClipCount = counts.accepted + counts.rejected;
    const acceptanceRate =
      statusReviewedClipCount > 0 ? counts.accepted / statusReviewedClipCount : null;
    return {
      total: qcScopedClips.length,
      reviewed: reviewedClipCount,
      predictedClipCount: acceptanceRate === null ? null : qcScopedClips.length * acceptanceRate,
      predictedDurationSeconds:
        reviewedDurationSeconds > 0
          ? totalDurationSeconds * (acceptedDurationSeconds / reviewedDurationSeconds)
          : null,
      rows,
      subsetCount: qcScopedClips.length,
      subsetDurationSeconds: totalDurationSeconds,
      meanDurationSeconds,
      medianDurationSeconds,
      standardDeviationSeconds: variance === null ? null : Math.sqrt(variance),
      minDurationSeconds: durations[0] ?? null,
      maxDurationSeconds: durations.at(-1) ?? null,
    };
  }, [qcScopedClips]);

  const humanLabeledClips = useMemo<HumanLabeledClip[]>(
    () =>
      SHOW_HUMAN_LABEL_OVERLAY
        ? clips
            .filter(
              (clip): clip is LabClip & { status: "accepted" | "rejected" } =>
                clip.status === "accepted" || clip.status === "rejected",
            )
            .map((clip) => ({
              clipId: clip.id,
              status: clip.status,
              transcriptMatch: clip.transcriptMatchRaw,
              speakerCheck: clip.speakerCheckRaw,
              cleanAccepted:
                clip.status === "accepted" &&
                clip.transcript === clip.originalTranscript &&
                (clip.audioEditOpCount ?? 0) === 0 &&
                clip.edits.length === 0,
            }))
        : [],
    [clips],
  );

  const updateClip = useCallback((id: string, fn: (clip: LabClip) => LabClip) => {
    setClips((prev) => prev.map((clip) => (clip.id === id ? fn(clip) : clip)));
  }, []);

  // Refetch the authoritative clip-lab view and reseed (used after a 409 stale).
  const reseedFromServer = useCallback(async () => {
    if (!runId) return;
    const fresh = await fetchClipLabView(runId);
    const mapped = fresh.clips.map((clip, index) =>
      mapApiClip(clip, index, runId, fresh.candidate_manifest_sha256),
    );
    queryClient.setQueryData(["sc-cliplab", runId], fresh);
    setClips(mapped);
  }, [runId, queryClient]);

  // Optimistic write: apply locally, call backend, reconcile with the returned
  // authoritative clip (crucially, its new clip_version). On a stale 409,
  // reload the view; on any other failure, revert and surface the error.
  const applyClipWrite = useCallback(
    async (
      clipId: string,
      optimistic: (clip: LabClip) => LabClip,
      call: (clip: LabClip) => Promise<DatasetClipLabClipView>,
      label: string,
    ): Promise<boolean> => {
      const snapshot = clips.find((c) => c.id === clipId);
      if (!snapshot) return false;
      updateClip(clipId, optimistic);
      if (!isLiveBacked(snapshot)) return true; // mock / not-yet-live: local only
      try {
        const server = await call(snapshot);
        updateClip(clipId, (c) => mergeClipLabWriteResponse(c, server));
        return true;
      } catch (err) {
        if (err instanceof SpeechcraftApiError && err.isStale) {
          toast({
            title: "Clip changed elsewhere",
            description: "Reloaded the latest version — reapply your change.",
            variant: "error",
            duration: 3500,
          });
          await reseedFromServer();
          return false;
        }
        updateClip(clipId, () => snapshot); // revert
        toast({
          title: `${label} failed`,
          description:
            err instanceof SpeechcraftApiError ? err.detail : String(err),
          variant: "error",
          duration: 4000,
        });
        return false;
      }
    },
    [clips, updateClip, toast, reseedFromServer],
  );

  const nextClipIdRef = useRef<string | null>(null);
  nextClipIdRef.current = (() => {
    if (!activeClipId) return null;
    const idx = visibleClips.findIndex((c) => c.id === activeClipId);
    return idx >= 0 ? (visibleClips[idx + 1]?.id ?? null) : null;
  })();

  const decide = useCallback(
    (status: ReviewStatus) => {
      if (!activeClipId) return;
      const nextId = nextClipIdRef.current;
      void applyClipWrite(
        activeClipId,
        (clip) => ({
          ...clip,
          status,
          revisions: [
            ...clip.revisions,
            {
              id: `${clip.id}-rev-${clip.revisions.length}`,
              message:
                status === "accepted"
                  ? "Accepted milestone"
                  : status === "rejected"
                    ? "Rejected milestone"
                    : `${status} milestone`,
              status,
              transcript: clip.transcript,
              createdAt: new Date().toISOString(),
              milestone: true,
            },
          ],
        }),
        (clip) =>
          patchClipLab(runId!, clip.id, {
            ...tokensFor(clip),
            review_status: status,
          }),
        "Status update",
      );
      if (nextId) {
        setAutoplayClipId(nextId);
        setActiveClipId(nextId);
      }
    },
    [activeClipId, applyClipWrite, runId],
  );

  const commitStatus = useCallback(
    (status: ReviewStatus) => {
      if (!activeClipId) return;
      void applyClipWrite(
        activeClipId,
        (clip) => ({ ...clip, status }),
        (clip) =>
          patchClipLab(runId!, clip.id, { ...tokensFor(clip), review_status: status }),
        "Status update",
      );
    },
    [activeClipId, applyClipWrite, runId],
  );

  const commitTranscript = useCallback(
    (text: string) => {
      if (!activeClipId) return;
      void applyClipWrite(
        activeClipId,
        (clip) => ({ ...clip, transcript: text }),
        (clip) =>
          patchClipLab(runId!, clip.id, {
            ...tokensFor(clip),
            transcript_override: text,
          }),
        "Transcript save",
      );
    },
    [activeClipId, applyClipWrite, runId],
  );

  const commitTags = useCallback(
    (tags: string[]) => {
      if (!activeClipId) return;
      void applyClipWrite(
        activeClipId,
        (clip) => ({ ...clip, tags }),
        (clip) =>
          patchClipLab(runId!, clip.id, { ...tokensFor(clip), reviewer_tags: tags }),
        "Tag update",
      );
    },
    [activeClipId, applyClipWrite, runId],
  );

  const navigate = useCallback(
    (direction: "prev" | "next") => {
      if (!activeClipId) return;
      const idx = visibleClips.findIndex((c) => c.id === activeClipId);
      if (idx === -1) return;
      const target = visibleClips[idx + (direction === "next" ? 1 : -1)];
      if (target) setActiveClipId(target.id);
    },
    [activeClipId, visibleClips],
  );

  const appendEdit = useCallback(
    (edit: ClipEdit) => {
      toast({
        title: "Not supported yet",
        description: `"${edit.op}" isn't a backend audio operation.`,
        variant: "error",
        duration: 3000,
      });
    },
    [toast],
  );

  const commitAudioOp = useCallback(
    async (operation: DatasetAudioEditOperation): Promise<boolean> => {
      if (!activeClipId) return false;
      return applyClipWrite(
        activeClipId,
        (c) => c,
        (c) => appendAudioOperation(runId!, c.id, { ...tokensFor(c), operation }),
        "Audio edit",
      );
    },
    [activeClipId, applyClipWrite, runId],
  );

  const undoEdit = useCallback(() => {
    if (!activeClipId) return;
    void runAudioEdit(() =>
      applyClipWrite(
        activeClipId,
        (c) => c,
        (c) => undoAudioOperation(runId!, c.id, tokensFor(c)),
        "Undo",
      ),
    );
  }, [activeClipId, applyClipWrite, runId, runAudioEdit]);

  const redoEdit = useCallback(() => {
    if (!activeClipId) return;
    void runAudioEdit(() =>
      applyClipWrite(
        activeClipId,
        (c) => c,
        (c) => redoAudioOperation(runId!, c.id, tokensFor(c)),
        "Redo",
      ),
    );
  }, [activeClipId, applyClipWrite, runId, runAudioEdit]);

  const markReference = useCallback(async (): Promise<string | null> => {
    const clip = clips.find((c) => c.id === activeClipId);
    if (!clip || !projectId || !runId) return null;
    try {
      const result = await markReferenceClipCandidate(projectId, runId, {
        clip_id: clip.id,
        transcript_text: clip.transcript,
      });
      toast({ title: "Saved as reference clip candidate", variant: "success", duration: 2000 });
      return result.folder_path;
    } catch (err) {
      toast({
        title: "Save reference clip failed",
        description: err instanceof SpeechcraftApiError ? err.detail : String(err),
        variant: "error",
        duration: 4000,
      });
      return null;
    }
  }, [clips, activeClipId, projectId, runId, toast]);

  const applyQcSubsetView = useCallback((nextView: { active_qc_subset: QcSubset | null }) => {
    const nextSubset = nextView.active_qc_subset;
    setActiveQcSubset(nextSubset);
    setSearch("");
    setFilterStatuses([]);
    setFilterTags([]);
    setFilterBuckets([]);
    setSortMode("source");
    const nextVisible = clips.filter((clip) => passesQcSubset(clip, nextSubset));
    setActiveClipId(nextVisible[0]?.id ?? null);
  }, [clips]);

  const openQcSubset = useCallback(async (
    thresholds: { transcript_match_min: number; speaker_check_min: number },
  ): Promise<void> => {
    if (useMockClips) {
      setDraftQcThresholds(thresholds);
      applyQcSubsetView({ active_qc_subset: thresholds });
      setMode("lab");
      return;
    }
    if (!runId) throw new Error("No dataset run selected");
    const nextView = await setQcSubset(runId, thresholds);
    setDraftQcThresholds(thresholds);
    queryClient.setQueryData(["sc-cliplab", runId], nextView);
    applyQcSubsetView(nextView);
    setMode("lab");
  }, [applyQcSubsetView, queryClient, runId, useMockClips]);

  const resetQcSubset = useCallback(async (): Promise<void> => {
    if (useMockClips) {
      applyQcSubsetView({ active_qc_subset: null });
      return;
    }
    if (!runId) return;
    const nextView = await clearQcSubset(runId);
    queryClient.setQueryData(["sc-cliplab", runId], nextView);
    applyQcSubsetView(nextView);
  }, [applyQcSubsetView, queryClient, runId, useMockClips]);

  const runModel = useCallback(() => {
    if (!activeClip) return;
    const clipId = activeClip.id;
    const { id } = toast({
      title: "Running DeepFilterNet",
      description: "Enhancing slice audio…",
      variant: "progress",
      progress: 0,
      duration: Number.POSITIVE_INFINITY,
    });
    const started = Date.now();
    const interval = setInterval(() => {
      const pct = Math.min(100, Math.round(((Date.now() - started) / 3500) * 100));
      update(id, { id, progress: pct });
      if (pct >= 100) {
        clearInterval(interval);
        updateClip(clipId, (clip) => ({ ...clip, variant: "deepfilternet" }));
        update(id, {
          id,
          title: "Enhancement complete",
          description: "Activated the DeepFilterNet variant.",
          variant: "success",
          duration: 2500,
        });
      }
    }, 120);
  }, [activeClip, toast, update, updateClip]);

  const hotkeyEnabled = mode === "lab" && !!activeClipId;
  const audioHotkeyEnabled = hotkeyEnabled && !audioEditInFlight;
  useHotkeys("enter", (e) => { e.preventDefault(); decide("accepted"); }, { enabled: hotkeyEnabled }, [decide]);
  useHotkeys("shift+enter", (e) => { e.preventDefault(); decide("rejected"); }, { enabled: hotkeyEnabled }, [decide]);
  useHotkeys("up", (e) => { e.preventDefault(); navigate("prev"); }, { enabled: hotkeyEnabled }, [navigate]);
  useHotkeys("down", (e) => { e.preventDefault(); navigate("next"); }, { enabled: hotkeyEnabled }, [navigate]);
  useHotkeys("mod+z", (e) => { e.preventDefault(); undoEdit(); }, { enabled: audioHotkeyEnabled }, [undoEdit]);
  useHotkeys("mod+shift+z", (e) => { e.preventDefault(); redoEdit(); }, { enabled: audioHotkeyEnabled }, [redoEdit]);

  const isLoading =
    !useMockClips &&
    ((!projectId && projects.length === 0) || runsLoading || (viewLoading && clips.length === 0));

  const undoAvailable = activeClip
    ? isLiveBacked(activeClip)
      ? !!activeClip.canUndoAudio
      : activeClip.edits.length > 0
    : false;
  const redoAvailable = activeClip
    ? isLiveBacked(activeClip)
      ? !!activeClip.canRedoAudio
      : false
    : false;

  return (
    <div className="flex h-screen flex-col">
      <header className="flex h-[70px] flex-shrink-0 items-center justify-between border-b border-border px-6">
        <div className="flex items-center gap-4">
          <Link href="/" aria-label="Home" className="flex items-center text-foreground">
            <Icons.LogoSmall />
          </Link>
          <div className="inline-flex border border-border p-0.5">
            <button
              type="button"
              onClick={() => setMode("qc")}
              className={cn(
                "px-3 py-1.5 text-sm transition-colors",
                mode === "qc" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              Dataset Health
            </button>
            <button
              type="button"
              onClick={() => setMode("lab")}
              className={cn(
                "px-3 py-1.5 text-sm transition-colors",
                mode === "lab" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground",
              )}
            >
              Clip Lab
            </button>
          </div>
          <div>
            <h1 className="font-serif text-lg leading-none">
              {mode === "lab" ? "Clip Lab" : "Dataset Health"}
            </h1>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {mode === "lab"
                ? "Manual slice review, transcript repair, and human overrides."
                : "Machine triage and yield tuning."}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 gap-1.5"
            onClick={() => setExportOpen(true)}
          >
            <Icons.Share className="size-4" />
            Export
          </Button>
          <LabRerunDialog projectId={projectId} runId={runId} />
          <DiagnosticsSheet
            runId={runId}
            runStatus={run?.status}
            runStage={run?.stage}
          />
          <ProjectPicker />
        </div>
      </header>

      <ExportDialog runId={runId} open={exportOpen} onOpenChange={setExportOpen} />

      {mode === "qc" ? (
        <DatasetHealthPage
          runId={runId}
          demo={useMockClips}
          humanLabeledClips={useMockClips ? [] : humanLabeledClips}
          activeQcSubset={activeQcSubset}
          draftQcThresholds={draftQcThresholds}
          onDraftQcThresholdsChange={setDraftQcThresholds}
          onOpenInClipLab={openQcSubset}
        />
      ) : viewError ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="max-w-sm text-center">
            <p className="font-serif text-xl">Backend unavailable</p>
            <p className="mt-2 text-sm text-muted-foreground">
              Couldn't reach the speechcraft backend. Make sure it's running on
              :8010 (make dev-backend), then reload.
            </p>
          </div>
        </div>
      ) : isLoading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          Loading clips…
        </div>
      ) : clips.length === 0 ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">
          No candidate clips in this project's dataset run yet.
        </div>
      ) : (
        <div className="flex flex-1 overflow-hidden">
          <ClipQueue
            clips={visibleClips}
            activeClipId={activeClipId}
            search={search}
            onSearchChange={setSearch}
            statuses={filterStatuses}
            tags={filterTags}
            availableTags={availableTags}
            buckets={filterBuckets}
            onToggleStatus={(status) =>
              setFilterStatuses((cur) =>
                cur.includes(status) ? cur.filter((s) => s !== status) : [...cur, status],
              )
            }
            onToggleTag={(tag) =>
              setFilterTags((cur) =>
                cur.includes(tag) ? cur.filter((t) => t !== tag) : [...cur, tag],
              )
            }
            onToggleBucket={(bucket) =>
              setFilterBuckets((cur) =>
                cur.includes(bucket) ? cur.filter((b) => b !== bucket) : [...cur, bucket],
              )
            }
            onClearFilters={() => {
              setFilterStatuses([]);
              setFilterTags([]);
              setFilterBuckets([]);
            }}
            sortMode={sortMode}
            onSortModeChange={setSortMode}
            onSelect={setActiveClipId}
          />

          <div className="flex flex-1 flex-col overflow-hidden">
            <div className="flex-1 space-y-4 overflow-y-auto p-4">
              {activeClip ? (
                <>
                  <ClipLabPanel
                    clip={activeClip}
                    canUndo={undoAvailable}
                    canRedo={redoAvailable}
                    onAccept={() => decide("accepted")}
                    onReject={() => decide("rejected")}
                    onAppendEdit={appendEdit}
                    onCommitAudioOp={commitAudioOp}
                    audioEditInFlight={audioEditInFlight}
                    tryBeginAudioEdit={tryBeginAudioEdit}
                    endAudioEdit={endAudioEdit}
                    onUndo={undoEdit}
                    onRedo={redoEdit}
                    onRunModel={runModel}
                    autoplay={autoplayClipId === activeClip.id}
                    onAutoplayConsumed={() => setAutoplayClipId(null)}
                  />
                  <TranscriptPanel
                    clip={activeClip}
                    allTags={allTags}
                    onTranscriptChange={commitTranscript}
                    onTagsChange={commitTags}
                  />
                </>
              ) : (
                <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                  Select a clip to begin review.
                </div>
              )}
            </div>
            <KeyboardBar />
          </div>

          {activeClip ? (
            <InspectorRail
              clip={activeClip}
              stats={stats}
              onStatusChange={commitStatus}
              onSaveReference={markReference}
              activeQcSubset={activeQcSubset}
              onResetQcSubset={resetQcSubset}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}
