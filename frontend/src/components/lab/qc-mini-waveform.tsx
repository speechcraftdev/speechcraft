"use client";

import { cn } from "@midday/ui/cn";
import { Icons } from "@midday/ui/icons";
import { useEffect, useRef, useState } from "react";

let activeQcPlayer: HTMLAudioElement | null = null;

type QcMiniWaveformProps = { audioUrl: string | null; label: string };

function clock(seconds: number): string {
  if (!Number.isFinite(seconds)) return "0:00";
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function drawWaveform(canvas: HTMLCanvasElement, peaks: Float32Array | null, progress: number): void {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * dpr));
  const height = Math.max(1, Math.round(rect.height * dpr));
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, width, height);
  const columns = Math.max(1, Math.round(rect.width));
  const mid = height / 2;
  const playedX = Math.round(Math.max(0, Math.min(1, progress)) * width);
  for (let x = 0; x < columns; x++) {
    const start = peaks ? Math.floor((x / columns) * peaks.length) : 0;
    const end = peaks ? Math.max(start + 1, Math.ceil(((x + 1) / columns) * peaks.length)) : 1;
    let peak = peaks ? 0 : 0.24 + (((x * 17) % 23) / 23) * 0.22;
    if (peaks) for (let i = start; i < Math.min(end, peaks.length); i++) peak = Math.max(peak, peaks[i]);
    const bar = Math.max(2, peak * height * 0.92);
    const x0 = Math.round(x * dpr);
    ctx.fillStyle = x0 <= playedX ? "rgba(232,232,232,.82)" : "rgba(125,125,125,.42)";
    ctx.fillRect(x0, Math.round(mid - bar / 2), Math.max(1, Math.round(dpr)), Math.round(bar));
  }
  if (progress > 0 && progress < 1) {
    ctx.fillStyle = "rgba(255,255,255,.95)";
    ctx.fillRect(Math.max(0, playedX - Math.round(dpr / 2)), 0, Math.max(1, Math.round(dpr)), height);
  }
}

export function QcMiniWaveform({ audioUrl, label }: QcMiniWaveformProps) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const peaksRef = useRef<Float32Array | null>(null);
  const progressRef = useRef(0);
  const animationRef = useRef<number | null>(null);
  const playingRef = useRef(false);
  const lastUiUpdateRef = useRef(0);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [current, setCurrent] = useState(0);
  const [loading, setLoading] = useState(Boolean(audioUrl));

  useEffect(() => {
    if (!audioUrl) return;
    const audio = new Audio(audioUrl);
    audio.preload = "metadata";
    audioRef.current = audio;
    const repaint = (updateUi = true) => {
      const nextDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
      const nextCurrent = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
      if (updateUi) {
        setDuration(nextDuration);
        setCurrent(nextCurrent);
      }
      progressRef.current = nextDuration > 0 ? nextCurrent / nextDuration : 0;
      if (canvasRef.current) drawWaveform(canvasRef.current, peaksRef.current, progressRef.current);
    };
    const tick = (now: number) => {
      if (!playingRef.current) return;
      repaint(false);
      if (now - lastUiUpdateRef.current > 80) {
        const nextCurrent = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
        setCurrent(nextCurrent);
        lastUiUpdateRef.current = now;
      }
      animationRef.current = requestAnimationFrame(tick);
    };
    const onPlay = () => {
      if (activeQcPlayer && activeQcPlayer !== audio) activeQcPlayer.pause();
      activeQcPlayer = audio;
      playingRef.current = true;
      setPlaying(true);
      if (animationRef.current === null) animationRef.current = requestAnimationFrame(tick);
    };
    const onPause = () => {
      playingRef.current = false;
      setPlaying(false);
      if (animationRef.current !== null) cancelAnimationFrame(animationRef.current);
      animationRef.current = null;
      repaint();
    };
    const onEnded = () => {
      playingRef.current = false;
      setPlaying(false);
      if (animationRef.current !== null) cancelAnimationFrame(animationRef.current);
      animationRef.current = null;
      progressRef.current = 0;
      repaint();
    };
    const onLoaded = () => { setLoading(false); repaint(); };
    audio.addEventListener("loadedmetadata", onLoaded);
    audio.addEventListener("durationchange", () => repaint());
    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    const resize = new ResizeObserver(() => {
      if (canvasRef.current) drawWaveform(canvasRef.current, peaksRef.current, progressRef.current);
    });
    if (canvasRef.current) resize.observe(canvasRef.current);
    let cancelled = false;
    fetch(audioUrl)
      .then((response) => response.arrayBuffer())
      .then(async (buffer) => {
        const context = new AudioContext();
        try {
          const decoded = await context.decodeAudioData(buffer);
          if (cancelled) return;
          const channel = decoded.getChannelData(0);
          const columns = 960;
          const peaks = new Float32Array(columns);
          for (let i = 0; i < columns; i++) {
            const start = Math.floor((i / columns) * channel.length);
            const end = Math.max(start + 1, Math.ceil(((i + 1) / columns) * channel.length));
            let max = 0;
            for (let j = start; j < Math.min(end, channel.length); j++) max = Math.max(max, Math.abs(channel[j]));
            peaks[i] = max;
          }
          peaksRef.current = peaks;
          if (canvasRef.current) drawWaveform(canvasRef.current, peaks, progressRef.current);
        } finally {
          void context.close();
        }
      })
      .catch(() => setLoading(false));
    return () => {
      cancelled = true;
      resize.disconnect();
      playingRef.current = false;
      if (animationRef.current !== null) cancelAnimationFrame(animationRef.current);
      audio.pause();
      audio.src = "";
      if (activeQcPlayer === audio) activeQcPlayer = null;
    };
  }, [audioUrl]);

  const seek = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const audio = audioRef.current;
    if (!audio || !duration) return;
    const rect = event.currentTarget.getBoundingClientRect();
    audio.currentTime = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * duration;
    progressRef.current = audio.currentTime / duration;
    if (canvasRef.current) drawWaveform(canvasRef.current, peaksRef.current, progressRef.current);
  };

  return (
    <div className="min-w-0">
      <div className="flex h-[56px] items-center gap-2">
        <button type="button" className="flex size-7 shrink-0 items-center justify-center rounded-full text-white transition-colors hover:bg-white/10 disabled:opacity-40" onClick={() => {
          const audio = audioRef.current;
          if (!audio) return;
          if (audio.paused) void audio.play(); else audio.pause();
        }} disabled={!audioUrl} aria-label={`${playing ? "Pause" : "Play"} ${label}`}>
          {playing ? <Icons.Pause className="size-4" /> : <Icons.Play className="size-4" />}
        </button>
        <div className="min-w-0 flex-1">
          <canvas ref={canvasRef} className={cn("block h-10 w-full cursor-pointer", loading && "opacity-70")} onPointerDown={seek} aria-label={`Seek ${label}`} />
        </div>
        <div className="w-[62px] shrink-0 text-right text-[11px] tabular-nums text-[#878787]">
          {clock(current)}/{clock(duration)}
        </div>
      </div>
    </div>
  );
}
