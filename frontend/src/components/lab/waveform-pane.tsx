"use client";

import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import RegionsPlugin from "wavesurfer.js/dist/plugins/regions.esm.js";

type WaveformPaneProps = {
  audioUrl: string;
  durationSeconds?: number;
  selectionStart: number;
  selectionEnd: number;
  onSelectionChange: (start: number, end: number) => void;
  onCursorChange: (time: number) => void;
  onHoverChange?: (time: number | null) => void;
  onReady?: (instance: WaveSurfer | null) => void;
  onAudioReady?: (instance: WaveSurfer) => void;
  onPlayingChange?: (isPlaying: boolean) => void;
};

/** Snap to clip start/end when the pointer is this close to either edge. */
const EDGE_SNAP_PX = 8;
/** Ignore sub-millisecond jitter when syncing the region from React state. */
const REGION_SYNC_EPS = 0.001;

// Adapted from speechcraft's WaveformPane — real wavesurfer + regions, but
// restyled to Midday's monochrome ink palette (was teal) and simplified to
// decode audio directly (no cached-peaks machinery). Keeps the core
// mechanics: click-to-seek, drag-to-select region, hover readout, ctrl+wheel
// zoom, and exposes the instance via onReady so the transport can drive it.
export function WaveformPane({
  audioUrl,
  durationSeconds,
  selectionStart,
  selectionEnd,
  onSelectionChange,
  onCursorChange,
  onHoverChange,
  onReady,
  onAudioReady,
  onPlayingChange,
}: WaveformPaneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const waveSurferRef = useRef<WaveSurfer | null>(null);
  const regionsRef = useRef<ReturnType<typeof RegionsPlugin.create> | null>(null);
  const zoomRef = useRef(0);
  const pointerDownRef = useRef(false);
  const pointerStartXRef = useRef<number | null>(null);
  const pointerStartTimeRef = useRef<number | null>(null);
  const draggedRef = useRef(false);
  const durationSecondsRef = useRef(durationSeconds);
  durationSecondsRef.current = durationSeconds;

  const selectionChangeRef = useRef(onSelectionChange);
  const cursorChangeRef = useRef(onCursorChange);
  const hoverChangeRef = useRef(onHoverChange);
  const readyRef = useRef(onReady);
  const audioReadyRef = useRef(onAudioReady);
  const playingChangeRef = useRef(onPlayingChange);
  selectionChangeRef.current = onSelectionChange;
  cursorChangeRef.current = onCursorChange;
  hoverChangeRef.current = onHoverChange;
  readyRef.current = onReady;
  audioReadyRef.current = onAudioReady;
  playingChangeRef.current = onPlayingChange;

  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const isAbortLike = (err: unknown) => {
    const msg = (err instanceof Error ? err.message : String(err ?? "")).toLowerCase();
    return msg.includes("abort") || msg.includes("cancel");
  };

  // Create the instance once.
  useEffect(() => {
    if (!containerRef.current) return;

    const isDark = document.documentElement.classList.contains("dark");
    const regions = RegionsPlugin.create();
    const ws = WaveSurfer.create({
      container: containerRef.current,
      height: 200,
      normalize: true,
      waveColor: isDark ? "#454545" : "#cfcfcf",
      progressColor: isDark ? "#9a9a9a" : "#6b6b6b",
      cursorColor: isDark ? "#f5f5f5" : "#171717",
      cursorWidth: 1,
      barWidth: 2,
      barGap: 1,
      dragToSeek: false,
      interact: false,
      autoScroll: false,
      autoCenter: false,
      plugins: [regions],
    });

    waveSurferRef.current = ws;
    regionsRef.current = regions;
    readyRef.current?.(ws);

    const clipDuration = (): number => {
      const decoded = ws.getDuration();
      const fromClip = durationSecondsRef.current;
      if (typeof fromClip === "number" && Number.isFinite(fromClip) && fromClip > 0) {
        return Math.max(decoded, fromClip);
      }
      return decoded;
    };

    const timeAtClientX = (clientX: number): number | null => {
      const wrapper = ws.getWrapper();
      const decoded = ws.getDuration();
      if (!wrapper || decoded <= 0) return null;
      const rect = wrapper.getBoundingClientRect();
      if (rect.width <= 0) return null;
      const duration = clipDuration();
      const x = clientX - rect.left;
      // Bar rendering leaves a few empty pixels after the last bar, and
      // dragging onto the container padding used to stop short of duration.
      if (x <= EDGE_SNAP_PX) return 0;
      if (x >= rect.width - EDGE_SNAP_PX) return duration;
      return Math.min(duration, Math.max(0, (x / rect.width) * decoded));
    };

    const seekTo = (time: number) => {
      const duration = clipDuration();
      const clamped = Math.min(duration, Math.max(0, time));
      ws.setTime(Math.min(ws.getDuration() || clamped, clamped));
      cursorChangeRef.current(clamped);
    };

    ws.on("timeupdate", (t) => {
      if (ws.isPlaying()) cursorChangeRef.current(t);
    });
    ws.on("play", () => playingChangeRef.current?.(true));
    ws.on("pause", () => playingChangeRef.current?.(false));
    ws.on("finish", () => playingChangeRef.current?.(false));
    ws.on("ready", () => {
      setState("ready");
      setError(null);
      audioReadyRef.current?.(ws);
    });
    ws.on("error", (err) => {
      if (isAbortLike(err)) return;
      setState("error");
      setError(err instanceof Error ? err.message : "Audio failed to load.");
      playingChangeRef.current?.(false);
    });

    regions.on("region-created", (region) => {
      for (const other of regions.getRegions()) {
        if (other.id !== region.id) other.remove();
      }
      selectionChangeRef.current(region.start, region.end);
    });
    regions.on("region-updated", (region) => {
      const duration = clipDuration();
      const start = region.start <= 0.005 ? 0 : region.start;
      const end = region.end >= duration - 0.005 ? duration : region.end;
      selectionChangeRef.current(start, end);
    });

    const el = containerRef.current;
    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 0) return;
      pointerDownRef.current = true;
      pointerStartXRef.current = e.clientX;
      pointerStartTimeRef.current = timeAtClientX(e.clientX);
      draggedRef.current = false;
      try {
        el.setPointerCapture(e.pointerId);
      } catch {
        // Pointer capture can fail on some synthetic events; drag still works
        // while the cursor stays inside the waveform.
      }
    };
    const onPointerMove = (e: PointerEvent) => {
      const t = timeAtClientX(e.clientX);
      if (t !== null) hoverChangeRef.current?.(t);
      if (
        pointerDownRef.current &&
        pointerStartXRef.current !== null &&
        pointerStartTimeRef.current !== null &&
        t !== null
      ) {
        if (Math.abs(e.clientX - pointerStartXRef.current) > 4) {
          draggedRef.current = true;
          selectionChangeRef.current(
            Math.min(pointerStartTimeRef.current, t),
            Math.max(pointerStartTimeRef.current, t),
          );
        }
      }
    };
    const finishPointer = (e: PointerEvent) => {
      if (!pointerDownRef.current) return;
      const didDrag = draggedRef.current;
      const start = pointerStartTimeRef.current;
      const end = timeAtClientX(e.clientX);
      pointerDownRef.current = false;
      pointerStartXRef.current = null;
      pointerStartTimeRef.current = null;
      if (el.hasPointerCapture(e.pointerId)) {
        el.releasePointerCapture(e.pointerId);
      }
      if (didDrag && start !== null && end !== null) {
        selectionChangeRef.current(Math.min(start, end), Math.max(start, end));
        setTimeout(() => {
          draggedRef.current = false;
        }, 120);
        return;
      }
      draggedRef.current = false;
      if (end !== null) {
        seekTo(end);
        selectionChangeRef.current(end, end);
      }
    };
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      const base = zoomRef.current || 80;
      zoomRef.current = Math.max(20, Math.min(600, base + (e.deltaY < 0 ? 30 : -30)));
      ws.zoom(zoomRef.current);
    };
    const onLeave = () => {
      if (!pointerDownRef.current) hoverChangeRef.current?.(null);
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("pointerdown", onPointerDown);
    el.addEventListener("pointermove", onPointerMove);
    el.addEventListener("pointerup", finishPointer);
    el.addEventListener("pointercancel", finishPointer);
    el.addEventListener("pointerleave", onLeave);

    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("pointerdown", onPointerDown);
      el.removeEventListener("pointermove", onPointerMove);
      el.removeEventListener("pointerup", finishPointer);
      el.removeEventListener("pointercancel", finishPointer);
      el.removeEventListener("pointerleave", onLeave);
      readyRef.current?.(null);
      ws.destroy();
      waveSurferRef.current = null;
      regionsRef.current = null;
    };
  }, []);

  // Load audio when the URL changes.
  useEffect(() => {
    const ws = waveSurferRef.current;
    if (!ws || !audioUrl) return;
    setState("loading");
    setError(null);
    zoomRef.current = 0;
    void ws.load(audioUrl).catch((err) => {
      if (!isAbortLike(err)) {
        setState("error");
        setError(err instanceof Error ? err.message : "Audio failed to load.");
      }
    });
  }, [audioUrl]);

  // Reflect external selection into a region.
  useEffect(() => {
    const regions = regionsRef.current;
    if (!regions) return;
    const start = Math.min(selectionStart, selectionEnd);
    const end = Math.max(selectionStart, selectionEnd);
    const current = regions.getRegions()[0];
    if (end <= start + 0.01) {
      current?.remove();
      return;
    }
    if (!current) {
      regions.addRegion({
        start,
        end,
        color: "hsl(var(--primary) / 0.12)",
        drag: true,
        resize: true,
      });
    } else if (
      Math.abs(current.start - start) > REGION_SYNC_EPS ||
      Math.abs(current.end - end) > REGION_SYNC_EPS
    ) {
      current.setOptions({ start, end });
    }
  }, [selectionStart, selectionEnd]);

  return (
    <div className="relative">
      {/* lineHeight/fontSize 0: line-height is inherited into WaveSurfer's
          shadow DOM, where whitespace text nodes in its template would
          otherwise create phantom line boxes that inflate the scroll/wrapper
          height and push the main canvas below the (absolutely positioned)
          progress canvas — producing a vertical step at the playback cursor. */}
      <div
        ref={containerRef}
        className="w-full"
        style={{ lineHeight: 0, fontSize: 0 }}
        aria-label="Waveform editor"
      />
      {state !== "ready" ? (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-secondary/40 text-center"
          role={state === "error" ? "alert" : "status"}
        >
          <span className="text-sm">
            {state === "loading" ? "Loading audio…" : "Audio unavailable"}
          </span>
          {state === "error" ? (
            <span className="max-w-xs text-xs text-muted-foreground">{error}</span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
