"use client";

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type RefObject,
} from "react";
import { formatClock } from "../lab-data";
import type { DatasetAudioEditOperation } from "../speechcraft-write-api";
import { ClipLabPlayback, buildCutPreviewPcm } from "./playback";
import { assertClipLabWavMatchesManifest, parseClipLabPcm16MonoWav } from "./pcm16-wav";
import {
  clampSampleToClip,
  clickWaveform,
  dragSelect,
  extendSelection,
  fitView,
  insertSilence,
  insertSilenceSampleCount,
  mappingFromState,
  moveCursor,
  panView,
  resizeGrabbedEndpoint,
  rippleDelete,
  sampleFromX,
  samplesPerPixel,
  selectEntireClip,
  selectionEnd,
  selectionStart,
  shiftClick,
  snapSelectionToZeroCrossings,
  splicePcmDelete,
  splicePcmInsertSilence,
  zoomAround,
  zoomToSelection,
  type TimelineState,
  createTimelineState,
  endpointAtX,
} from "./timeline";
import {
  clipPeakAbs,
  drawOverlay,
  drawWaveform,
  readWaveformTheme,
  resizeCanvasToCss,
  type WaveformTheme,
} from "./waveform-draw";

const HEIGHT_CSS = 200;
const DRAG_THRESHOLD_PX = 4;
const EDGE_HIT_PX = 6;
const ZOOM_FACTOR = 1.25;

export type WaveformEditorHandle = {
  togglePlayback: () => void;
  playFromStart: () => void;
  stopPlayback: () => void;
  setPlaybackRate: (rate: number) => void;
  deleteSelection: () => Promise<void>;
  insertSilenceAtCursor: () => Promise<void>;
  focus: () => void;
};

export type WaveformStatusRefs = {
  selection: RefObject<HTMLElement | null>;
  time: RefObject<HTMLElement | null>;
};

type WaveformEditorProps = {
  audioUrl: string;
  sampleRateHz: number;
  expectedDurationSamples: number | null;
  playbackRate: number;
  autoplay: boolean;
  onAutoplayConsumed: () => void;
  statusRefs: WaveformStatusRefs;
  onPlayingChange: (playing: boolean) => void;
  onHasSelectionChange: (hasSelection: boolean) => void;
  tryBeginAudioEdit: () => boolean;
  endAudioEdit: () => void;
  onCommitAudioOp: (op: DatasetAudioEditOperation) => Promise<boolean>;
  onRefuseEntireClip: () => void;
};

type Gesture =
  | { kind: "idle" }
  | {
      kind: "pending";
      pointerId: number;
      startX: number;
      startSample: number;
      grabbed: "anchor" | "focus" | null;
      shiftKey: boolean;
    }
  | { kind: "selecting"; pointerId: number; anchorSample: number }
  | { kind: "resizing"; pointerId: number; grabbed: "anchor" | "focus" };

type EditorSnapshot = {
  state: TimelineState;
  pcm: Int16Array;
  peak: number;
};

function isDarkTheme(): boolean {
  return typeof document !== "undefined" && document.documentElement.classList.contains("dark");
}

function sampleToSeconds(sample: number, sampleRateHz: number): number {
  return sampleRateHz > 0 ? sample / sampleRateHz : 0;
}

export const WaveformEditor = forwardRef<WaveformEditorHandle, WaveformEditorProps>(
  function WaveformEditor(
    {
      audioUrl,
      sampleRateHz,
      expectedDurationSamples,
      playbackRate,
      autoplay,
      onAutoplayConsumed,
      statusRefs,
      onPlayingChange,
      onHasSelectionChange,
      tryBeginAudioEdit,
      endAudioEdit,
      onCommitAudioOp,
      onRefuseEntireClip,
    },
    ref,
  ) {
    const rootRef = useRef<HTMLDivElement | null>(null);
    const waveRef = useRef<HTMLCanvasElement | null>(null);
    const overlayRef = useRef<HTMLCanvasElement | null>(null);
    const stateRef = useRef<TimelineState>(createTimelineState(0, sampleRateHz));
    const pcmRef = useRef<Int16Array>(new Int16Array(0));
    const peakRef = useRef(1);
    const themeRef = useRef<WaveformTheme>(readWaveformTheme(isDarkTheme()));
    const sizeRef = useRef({ width: 0, height: HEIGHT_CSS, dpr: 1 });
    const rectRef = useRef<DOMRect | null>(null);
    const gestureRef = useRef<Gesture>({ kind: "idle" });
    const hoverSampleRef = useRef<number | null>(null);
    const hoverEdgeRef = useRef<"start" | "end" | null>(null);
    const playheadRef = useRef<number | null>(null);
    const lastPointerXRef = useRef(0);
    const autoScrollRafRef = useRef(0);
    const playbackRef = useRef(new ClipLabPlayback());
    const playingRef = useRef(false);
    const preserveTimelineRef = useRef(false);
    const snapshotRef = useRef<EditorSnapshot | null>(null);
    const loadGenRef = useRef(0);
    const autoplayRef = useRef(autoplay);
    const playbackRateRef = useRef(playbackRate);
    const expectedDurationRef = useRef(expectedDurationSamples);
    const sampleRateRef = useRef(sampleRateHz);
    const callbacksRef = useRef({
      onAutoplayConsumed,
      onPlayingChange,
      onHasSelectionChange,
      tryBeginAudioEdit,
      endAudioEdit,
      onCommitAudioOp,
      onRefuseEntireClip,
    });

    autoplayRef.current = autoplay;
    playbackRateRef.current = playbackRate;
    expectedDurationRef.current = expectedDurationSamples;
    sampleRateRef.current = sampleRateHz;
    callbacksRef.current = {
      onAutoplayConsumed,
      onPlayingChange,
      onHasSelectionChange,
      tryBeginAudioEdit,
      endAudioEdit,
      onCommitAudioOp,
      onRefuseEntireClip,
    };

    const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
    const [error, setError] = useState<string | null>(null);
    const readyRef = useRef(false);

    const cacheRect = () => {
      const el = rootRef.current;
      if (el) rectRef.current = el.getBoundingClientRect();
    };

    const mapping = () => mappingFromState(stateRef.current, sizeRef.current.width);

    const xFromClientX = (clientX: number): number => {
      const rect = rectRef.current;
      if (!rect) return 0;
      return clientX - rect.left;
    };

    const publishHasSelection = () => {
      callbacksRef.current.onHasSelectionChange(stateRef.current.selection !== null);
    };

    const writeStatus = () => {
      const state = stateRef.current;
      const sr = state.sampleRateHz || sampleRateRef.current;
      const selEl = statusRefs.selection.current;
      const timeEl = statusRefs.time.current;
      const hover = hoverSampleRef.current;
      const playhead = playheadRef.current;
      const spp = sizeRef.current.width > 0 ? samplesPerPixel(mapping()) : 1;
      const precise = spp < 2;
      if (selEl) {
        if (state.selection) {
          const a = selectionStart(state.selection);
          const b = selectionEnd(state.selection);
          selEl.textContent = precise
            ? `Sel ${a}–${b} (${b - a})`
            : `Sel ${formatClock(sampleToSeconds(a, sr))}–${formatClock(sampleToSeconds(b, sr))}`;
          selEl.classList.add("text-foreground");
        } else if (hover !== null) {
          selEl.textContent = precise
            ? `Hover ${hover}`
            : `Hover ${formatClock(sampleToSeconds(hover, sr))}`;
          selEl.classList.remove("text-foreground");
        } else {
          selEl.textContent = "Sel none";
          selEl.classList.remove("text-foreground");
        }
      }
      if (timeEl) {
        const shown = playhead ?? state.cursorSample;
        timeEl.textContent = `${formatClock(sampleToSeconds(shown, sr))} / ${formatClock(sampleToSeconds(state.durationSamples, sr))}`;
      }
    };

    const paintOverlay = () => {
      const canvas = overlayRef.current;
      const { width, height, dpr } = sizeRef.current;
      if (!canvas || width <= 0) return;
      const ctx = resizeCanvasToCss(canvas, width, height, dpr);
      if (!ctx) return;
      const state = stateRef.current;
      const sel = state.selection
        ? { startSample: selectionStart(state.selection), endSample: selectionEnd(state.selection) }
        : null;
      drawOverlay(
        ctx,
        mapping(),
        width,
        height,
        {
          cursorSample: state.cursorSample,
          selection: sel,
          playheadSample: playheadRef.current,
          hoverSample: hoverSampleRef.current,
          hoverEdge: hoverEdgeRef.current,
        },
        themeRef.current,
        dpr,
      );
      writeStatus();
    };

    const paintWaveform = () => {
      const canvas = waveRef.current;
      const { width, height, dpr } = sizeRef.current;
      if (!canvas || width <= 0) return;
      themeRef.current = readWaveformTheme(isDarkTheme());
      const ctx = resizeCanvasToCss(canvas, width, height, dpr);
      if (!ctx) return;
      drawWaveform(
        ctx,
        pcmRef.current,
        mapping(),
        width,
        height,
        peakRef.current,
        themeRef.current,
        dpr,
      );
      paintOverlay();
    };

    const measure = () => {
      const el = rootRef.current;
      if (!el) return;
      const width = el.getBoundingClientRect().width;
      const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
      if (Math.abs(width - sizeRef.current.width) < 0.01 && dpr === sizeRef.current.dpr) {
        cacheRect();
        return;
      }
      sizeRef.current = { width, height: HEIGHT_CSS, dpr };
      cacheRect();
      paintWaveform();
    };

    const stopAutoScroll = () => {
      if (autoScrollRafRef.current) {
        cancelAnimationFrame(autoScrollRafRef.current);
        autoScrollRafRef.current = 0;
      }
    };

    const stopPlayback = useCallback(() => {
      playbackRef.current.stop();
      playheadRef.current = null;
      if (playingRef.current) {
        playingRef.current = false;
        callbacksRef.current.onPlayingChange(false);
      }
      paintOverlay();
    }, []);

    const playRange = async (startSample: number, endSample: number) => {
      const pcm = pcmRef.current;
      const state = stateRef.current;
      if (endSample <= startSample || pcm.length === 0) return;
      playingRef.current = true;
      callbacksRef.current.onPlayingChange(true);
      playbackRef.current.setPlaybackRate(playbackRateRef.current);
      await playbackRef.current.playPcm(
        pcm,
        state.sampleRateHz,
        { kind: "linear", startSample, endSample },
        (sample) => {
          playheadRef.current = sample;
          paintOverlay();
        },
        () => {
          playheadRef.current = null;
          if (playingRef.current) {
            playingRef.current = false;
            callbacksRef.current.onPlayingChange(false);
          }
          paintOverlay();
        },
      );
    };

    const togglePlayback = () => {
      if (playingRef.current) {
        stopPlayback();
        return;
      }
      const state = stateRef.current;
      if (state.selection) {
        void playRange(selectionStart(state.selection), selectionEnd(state.selection));
        return;
      }
      void playRange(state.cursorSample, state.durationSamples);
    };

    const playFromStart = () => {
      stopPlayback();
      void playRange(0, stateRef.current.durationSamples);
    };

    const playCutPreview = () => {
      const state = stateRef.current;
      if (!state.selection) return;
      const preview = buildCutPreviewPcm(
        pcmRef.current,
        selectionStart(state.selection),
        selectionEnd(state.selection),
        state.sampleRateHz,
      );
      if (!preview) return;
      stopPlayback();
      playingRef.current = true;
      callbacksRef.current.onPlayingChange(true);
      playbackRef.current.setPlaybackRate(playbackRateRef.current);
      void playbackRef.current.playPcm(
        preview.pcm,
        state.sampleRateHz,
        preview.transport,
        (sample) => {
          playheadRef.current = sample;
          paintOverlay();
        },
        () => {
          playheadRef.current = null;
          if (playingRef.current) {
            playingRef.current = false;
            callbacksRef.current.onPlayingChange(false);
          }
          paintOverlay();
        },
        { cacheOrdinaryBuffer: false },
      );
    };

    const applyPointerSample = (clientX: number) => {
      const width = sizeRef.current.width;
      const x = xFromClientX(clientX);
      lastPointerXRef.current = clientX;
      const raw = sampleFromX(x, mapping());
      const sample = clampSampleToClip(raw, stateRef.current.durationSamples);
      const gesture = gestureRef.current;
      if (gesture.kind === "selecting") {
        stateRef.current = dragSelect(stateRef.current, gesture.anchorSample, sample);
      } else if (gesture.kind === "resizing") {
        stateRef.current = resizeGrabbedEndpoint(stateRef.current, gesture.grabbed, sample);
      }
      hoverSampleRef.current = sample;
      paintOverlay();
    };

    const autoScrollTick = () => {
      const gesture = gestureRef.current;
      if (gesture.kind !== "selecting" && gesture.kind !== "resizing") {
        stopAutoScroll();
        return;
      }
      const rect = rectRef.current;
      const width = sizeRef.current.width;
      if (!rect || width <= 0) return;
      const x = lastPointerXRef.current - rect.left;
      const spp = samplesPerPixel(mapping());
      let pan = 0;
      if (x < 0) pan = Math.round(x * spp * 0.2);
      else if (x > width) pan = Math.round((x - width) * spp * 0.2);
      if (pan !== 0) {
        const before = stateRef.current;
        stateRef.current = panView(before, pan);
        if (
          stateRef.current.viewStartSample !== before.viewStartSample ||
          stateRef.current.viewEndSample !== before.viewEndSample
        ) {
          applyPointerSample(lastPointerXRef.current);
          paintWaveform();
        }
      }
      autoScrollRafRef.current = requestAnimationFrame(autoScrollTick);
    };

    const maybeStartAutoScroll = (clientX: number) => {
      const rect = rectRef.current;
      const width = sizeRef.current.width;
      if (!rect || width <= 0) return;
      const x = clientX - rect.left;
      const outside = x < 0 || x > width;
      if (outside && !autoScrollRafRef.current) {
        autoScrollRafRef.current = requestAnimationFrame(autoScrollTick);
      }
      if (!outside) stopAutoScroll();
    };

    const updateHoverCursor = (clientX: number) => {
      const root = rootRef.current;
      const state = stateRef.current;
      if (!root) return;
      const x = xFromClientX(clientX);
      hoverSampleRef.current = clampSampleToClip(sampleFromX(x, mapping()), state.durationSamples);
      hoverEdgeRef.current = null;
      if (state.selection) {
        const grabbed = endpointAtX(state.selection, mapping(), x, EDGE_HIT_PX);
        if (grabbed) {
          const startIsAnchor = state.selection.anchorSample <= state.selection.focusSample;
          hoverEdgeRef.current =
            (grabbed === "anchor") === startIsAnchor ? "start" : "end";
          root.style.cursor = "ew-resize";
        } else {
          root.style.cursor = "text";
        }
      } else {
        root.style.cursor = "text";
      }
      paintOverlay();
    };

    const commitEdit = async (op: DatasetAudioEditOperation, snapshot: EditorSnapshot) => {
      preserveTimelineRef.current = true;
      const ok = await callbacksRef.current.onCommitAudioOp(op);
      if (!ok) {
        stateRef.current = snapshot.state;
        pcmRef.current = snapshot.pcm;
        peakRef.current = snapshot.peak;
        preserveTimelineRef.current = false;
        paintWaveform();
        publishHasSelection();
      }
    };

    const deleteSelection = async () => {
      if (!callbacksRef.current.tryBeginAudioEdit()) return;
      try {
        stopPlayback();
        const result = rippleDelete(stateRef.current);
        if (!result.ok) {
          if (result.reason === "entire_clip") callbacksRef.current.onRefuseEntireClip();
          return;
        }
        const snapshot: EditorSnapshot = {
          state: stateRef.current,
          pcm: pcmRef.current,
          peak: peakRef.current,
        };
        snapshotRef.current = snapshot;
        pcmRef.current = splicePcmDelete(pcmRef.current, result.startSample, result.endSample);
        peakRef.current = clipPeakAbs(pcmRef.current);
        stateRef.current = result.state;
        paintWaveform();
        publishHasSelection();
        await commitEdit(
          { kind: "delete_range", start_sample: result.startSample, end_sample: result.endSample },
          snapshot,
        );
      } finally {
        callbacksRef.current.endAudioEdit();
      }
    };

    const insertSilenceAtCursor = async () => {
      if (!callbacksRef.current.tryBeginAudioEdit()) return;
      try {
        stopPlayback();
        const durationSamples = insertSilenceSampleCount(stateRef.current.sampleRateHz);
        if (durationSamples <= 0) return;
        const snapshot: EditorSnapshot = {
          state: stateRef.current,
          pcm: pcmRef.current,
          peak: peakRef.current,
        };
        const inserted = insertSilence(stateRef.current, durationSamples);
        pcmRef.current = splicePcmInsertSilence(pcmRef.current, inserted.atSample, inserted.durationSamples);
        peakRef.current = clipPeakAbs(pcmRef.current);
        stateRef.current = inserted.state;
        paintWaveform();
        publishHasSelection();
        await commitEdit(
          {
            kind: "insert_silence",
            at_sample: inserted.atSample,
            duration_samples: inserted.durationSamples,
          },
          snapshot,
        );
      } finally {
        callbacksRef.current.endAudioEdit();
      }
    };

    useImperativeHandle(ref, () => ({
      togglePlayback,
      playFromStart,
      stopPlayback,
      setPlaybackRate: (rate: number) => {
        playbackRateRef.current = rate;
        playbackRef.current.setPlaybackRate(rate);
      },
      deleteSelection,
      insertSilenceAtCursor,
      focus: () => rootRef.current?.focus(),
    }));

    useEffect(() => {
      playbackRef.current.setPlaybackRate(playbackRate);
    }, [playbackRate]);

    useEffect(() => {
      const el = rootRef.current;
      if (!el) return;
      const ro = new ResizeObserver(() => measure());
      ro.observe(el);
      measure();
      return () => ro.disconnect();
    }, []);

    useEffect(() => {
      const gen = ++loadGenRef.current;
      const keepTimeline = preserveTimelineRef.current;
      if (!keepTimeline) {
        setLoadState("loading");
        setError(null);
        readyRef.current = false;
        stopPlayback();
      }
      const ac = new AbortController();
      void (async () => {
        try {
          const res = await fetch(audioUrl, { signal: ac.signal });
          if (!res.ok) throw new Error(`Audio failed to load (${res.status}).`);
          const buffer = await res.arrayBuffer();
          if (gen !== loadGenRef.current) return;
          const parsed = parseClipLabPcm16MonoWav(buffer);
          const expected = expectedDurationRef.current;
          if (expected == null) {
            if (parsed.sampleRateHz !== sampleRateRef.current) {
              throw new Error(
                `sample rate mismatch: wav ${parsed.sampleRateHz} Hz vs manifest ${sampleRateRef.current} Hz`,
              );
            }
          } else {
            assertClipLabWavMatchesManifest(parsed, {
              sampleRateHz: sampleRateRef.current,
              durationSamples: expected,
            });
          }
          pcmRef.current = parsed.pcm;
          peakRef.current = clipPeakAbs(parsed.pcm);
          if (keepTimeline && stateRef.current.durationSamples === parsed.pcm.length) {
            stateRef.current = { ...stateRef.current, sampleRateHz: parsed.sampleRateHz };
          } else {
            stateRef.current = createTimelineState(parsed.pcm.length, parsed.sampleRateHz);
          }
          preserveTimelineRef.current = false;
          readyRef.current = true;
          setLoadState("ready");
          paintWaveform();
          publishHasSelection();
          if (autoplayRef.current && !keepTimeline) {
            callbacksRef.current.onAutoplayConsumed();
            void playRange(0, parsed.pcm.length);
          }
        } catch (err) {
          if (ac.signal.aborted || gen !== loadGenRef.current) return;
          preserveTimelineRef.current = false;
          if (keepTimeline && pcmRef.current.length > 0) return;
          readyRef.current = false;
          setLoadState("error");
          setError(err instanceof Error ? err.message : "Audio failed to load.");
        }
      })();
      return () => {
        ac.abort();
      };
    }, [audioUrl]);

    useEffect(() => {
      const el = rootRef.current;
      if (!el) return;

      const onPointerEnter = () => cacheRect();

      const onPointerDown = (e: PointerEvent) => {
        if (e.button !== 0) return;
        if (!readyRef.current && pcmRef.current.length === 0) return;
        e.preventDefault();
        el.focus();
        stopPlayback();
        cacheRect();
        try {
          el.setPointerCapture(e.pointerId);
        } catch {
          // capture is best-effort
        }
        const x = xFromClientX(e.clientX);
        const sample = clampSampleToClip(sampleFromX(x, mapping()), stateRef.current.durationSamples);
        lastPointerXRef.current = e.clientX;
        const grabbed = stateRef.current.selection
          ? endpointAtX(stateRef.current.selection, mapping(), x, EDGE_HIT_PX)
          : null;
        gestureRef.current = {
          kind: "pending",
          pointerId: e.pointerId,
          startX: e.clientX,
          startSample: sample,
          grabbed,
          shiftKey: e.shiftKey,
        };
      };

      const onPointerMove = (e: PointerEvent) => {
        if (!rectRef.current) cacheRect();
        const gesture = gestureRef.current;
        if (gesture.kind === "idle") {
          updateHoverCursor(e.clientX);
          return;
        }
        if (gesture.kind === "pending") {
          if (Math.abs(e.clientX - gesture.startX) <= DRAG_THRESHOLD_PX) {
            hoverSampleRef.current = clampSampleToClip(
              sampleFromX(xFromClientX(e.clientX), mapping()),
              stateRef.current.durationSamples,
            );
            paintOverlay();
            return;
          }
          if (gesture.grabbed) {
            gestureRef.current = { kind: "resizing", pointerId: e.pointerId, grabbed: gesture.grabbed };
          } else {
            gestureRef.current = {
              kind: "selecting",
              pointerId: e.pointerId,
              anchorSample: gesture.startSample,
            };
          }
        }
        applyPointerSample(e.clientX);
        maybeStartAutoScroll(e.clientX);
        const g = gestureRef.current;
        if (g.kind === "selecting" || g.kind === "resizing") publishHasSelection();
      };

      const finishPointer = (e: PointerEvent) => {
        const gesture = gestureRef.current;
        if (gesture.kind === "idle") return;
        stopAutoScroll();
        if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId);
        if (!rectRef.current) cacheRect();
        const sample = clampSampleToClip(
          sampleFromX(xFromClientX(e.clientX), mapping()),
          stateRef.current.durationSamples,
        );
        if (gesture.kind === "pending") {
          if (gesture.shiftKey || e.shiftKey) {
            stateRef.current = shiftClick(stateRef.current, sample);
          } else {
            stateRef.current = clickWaveform(stateRef.current, sample);
          }
        } else if (gesture.kind === "selecting") {
          stateRef.current = dragSelect(stateRef.current, gesture.anchorSample, sample);
        } else if (gesture.kind === "resizing") {
          stateRef.current = resizeGrabbedEndpoint(stateRef.current, gesture.grabbed, sample);
        }
        gestureRef.current = { kind: "idle" };
        hoverSampleRef.current = sample;
        publishHasSelection();
        paintOverlay();
      };

      const onPointerLeave = () => {
        if (gestureRef.current.kind === "idle") {
          hoverSampleRef.current = null;
          hoverEdgeRef.current = null;
          paintOverlay();
        }
      };

      const onDblClick = (e: MouseEvent) => {
        e.preventDefault();
        stopPlayback();
        stateRef.current = selectEntireClip(stateRef.current);
        publishHasSelection();
        paintOverlay();
      };

      const onWheel = (e: WheelEvent) => {
        if (pcmRef.current.length === 0 || sizeRef.current.width <= 0) return;
        cacheRect();
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          const x = xFromClientX(e.clientX);
          const sample = sampleFromX(x, mapping());
          const factor = e.deltaY < 0 ? ZOOM_FACTOR : 1 / ZOOM_FACTOR;
          stateRef.current = zoomAround(stateRef.current, sample, x, sizeRef.current.width, factor);
          paintWaveform();
          return;
        }
        const pixelDelta = e.deltaX !== 0 ? e.deltaX : e.deltaY;
        if (pixelDelta === 0) return;
        e.preventDefault();
        const panSamples = Math.round(pixelDelta * samplesPerPixel(mapping()));
        stateRef.current = panView(stateRef.current, panSamples);
        paintWaveform();
      };

      const onKeyDown = (e: KeyboardEvent) => {
        if (e.target !== el) return;
        const key = e.key;
        if (e.ctrlKey || e.metaKey) {
          if (key === "e" || key === "E") {
            e.preventDefault();
            stateRef.current = zoomToSelection(stateRef.current);
            paintWaveform();
          } else if (key === "f" || key === "F") {
            e.preventDefault();
            stateRef.current = fitView(stateRef.current);
            paintWaveform();
          }
          return;
        }
        if (key === "ArrowLeft" || key === "ArrowRight") {
          e.preventDefault();
          const delta = key === "ArrowLeft" ? -1 : 1;
          stateRef.current = e.shiftKey
            ? extendSelection(stateRef.current, delta)
            : moveCursor(stateRef.current, delta);
          publishHasSelection();
          paintOverlay();
          return;
        }
        if (key === "Delete" || key === "Backspace") {
          e.preventDefault();
          void deleteSelection();
          return;
        }
        if (key === " " || e.code === "Space") {
          e.preventDefault();
          if (e.shiftKey) playFromStart();
          else togglePlayback();
          return;
        }
        if (key === "c" || key === "C") {
          e.preventDefault();
          playCutPreview();
          return;
        }
        if (key === "z" || key === "Z") {
          e.preventDefault();
          if (!stateRef.current.selection) return;
          stateRef.current = snapSelectionToZeroCrossings(stateRef.current, pcmRef.current);
          publishHasSelection();
          paintOverlay();
        }
      };

      el.addEventListener("pointerenter", onPointerEnter);
      el.addEventListener("pointerdown", onPointerDown);
      el.addEventListener("pointermove", onPointerMove);
      el.addEventListener("pointerup", finishPointer);
      el.addEventListener("pointercancel", finishPointer);
      el.addEventListener("pointerleave", onPointerLeave);
      el.addEventListener("dblclick", onDblClick);
      el.addEventListener("wheel", onWheel, { passive: false });
      el.addEventListener("keydown", onKeyDown);
      return () => {
        el.removeEventListener("pointerenter", onPointerEnter);
        el.removeEventListener("pointerdown", onPointerDown);
        el.removeEventListener("pointermove", onPointerMove);
        el.removeEventListener("pointerup", finishPointer);
        el.removeEventListener("pointercancel", finishPointer);
        el.removeEventListener("pointerleave", onPointerLeave);
        el.removeEventListener("dblclick", onDblClick);
        el.removeEventListener("wheel", onWheel);
        el.removeEventListener("keydown", onKeyDown);
        stopAutoScroll();
      };
    }, []);

    useEffect(() => {
      return () => {
        playbackRef.current.stop();
        stopAutoScroll();
      };
    }, []);

    return (
      <div className="relative">
        <div
          ref={rootRef}
          tabIndex={0}
          role="application"
          aria-label="Waveform editor"
          className="relative w-full overflow-hidden outline-none focus-visible:ring-1 focus-visible:ring-foreground/40"
          style={{ height: HEIGHT_CSS, lineHeight: 0, fontSize: 0 }}
        >
          <canvas
            ref={waveRef}
            className="absolute inset-0 block h-full w-full [image-rendering:pixelated]"
          />
          <canvas
            ref={overlayRef}
            className="pointer-events-none absolute inset-0 block h-full w-full [image-rendering:pixelated]"
          />
        </div>
        {loadState !== "ready" ? (
          <div
            className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-secondary/40 text-center"
            role={loadState === "error" ? "alert" : "status"}
          >
            <span className="text-sm">
              {loadState === "loading" ? "Loading audio…" : "Audio unavailable"}
            </span>
            {loadState === "error" ? (
              <span className="max-w-xs text-xs text-muted-foreground">{error}</span>
            ) : null}
          </div>
        ) : null}
      </div>
    );
  },
);
