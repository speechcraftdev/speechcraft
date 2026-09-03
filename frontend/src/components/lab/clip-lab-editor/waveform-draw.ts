import {
  sampleRangeForPixelColumn,
  samplesPerPixel,
  xFromSample,
  type ViewMapping,
} from "./timeline";

export const POLYLINE_SAMPLES_PER_PIXEL = 1;
export const POINT_SAMPLES_PER_PIXEL = 0.35;

export type WaveformTheme = {
  wave: string;
  selectionFill: string;
  selectionEdge: string;
  cursor: string;
  playhead: string;
  progress: string;
};

export function clipPeakAbs(pcm: Int16Array): number {
  let peak = 1;
  for (let i = 0; i < pcm.length; i++) {
    const a = pcm[i] < 0 ? -pcm[i] : pcm[i];
    if (a > peak) peak = a;
  }
  return peak;
}

export function yFromPcmValue(value: number, heightCssPx: number, peak: number): number {
  const mid = heightCssPx / 2;
  const n = value / (peak > 0 ? peak : 1);
  return mid - n * (mid - 1);
}

export function resizeCanvasToCss(
  canvas: HTMLCanvasElement,
  widthCssPx: number,
  heightCssPx: number,
  devicePixelRatio: number,
): CanvasRenderingContext2D | null {
  const dpr = devicePixelRatio > 0 ? devicePixelRatio : 1;
  const width = Math.max(1, Math.round(widthCssPx * dpr));
  const height = Math.max(1, Math.round(heightCssPx * dpr));
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  canvas.style.width = `${widthCssPx}px`;
  canvas.style.height = `${heightCssPx}px`;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

function drawMinMax(
  ctx: CanvasRenderingContext2D,
  pcm: Int16Array,
  mapping: ViewMapping,
  widthCssPx: number,
  heightCssPx: number,
  peak: number,
): void {
  const columns = Math.max(1, Math.ceil(widthCssPx));
  ctx.beginPath();
  for (let x = 0; x < columns; x++) {
    const { start, end } = sampleRangeForPixelColumn(x, mapping);
    const i0 = Math.max(0, Math.floor(start));
    const i1 = Math.min(pcm.length, Math.max(i0 + 1, Math.ceil(end)));
    if (i0 >= pcm.length) continue;
    let min = pcm[i0];
    let max = pcm[i0];
    for (let i = i0 + 1; i < i1; i++) {
      const v = pcm[i];
      if (v < min) min = v;
      if (v > max) max = v;
    }
    const y0 = yFromPcmValue(max, heightCssPx, peak);
    const y1 = yFromPcmValue(min, heightCssPx, peak);
    const cx = x + 0.5;
    ctx.moveTo(cx, y0);
    ctx.lineTo(cx, y1 === y0 ? y0 + 1 : y1);
  }
  ctx.stroke();
}

function drawPolyline(
  ctx: CanvasRenderingContext2D,
  pcm: Int16Array,
  mapping: ViewMapping,
  heightCssPx: number,
  peak: number,
  withPoints: boolean,
): void {
  const start = Math.max(0, Math.floor(mapping.viewStartSample));
  const end = Math.min(pcm.length, Math.ceil(mapping.viewEndSample) + 1);
  if (end <= start) return;
  ctx.beginPath();
  for (let i = start; i < end; i++) {
    const x = xFromSample(i, mapping);
    const y = yFromPcmValue(pcm[i], heightCssPx, peak);
    if (i === start) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  if (!withPoints) return;
  ctx.beginPath();
  for (let i = start; i < end; i++) {
    const x = xFromSample(i, mapping);
    const y = yFromPcmValue(pcm[i], heightCssPx, peak);
    ctx.moveTo(x + 1.5, y);
    ctx.arc(x, y, 1.5, 0, Math.PI * 2);
  }
  ctx.fill();
}

export function drawWaveform(
  ctx: CanvasRenderingContext2D,
  pcm: Int16Array,
  mapping: ViewMapping,
  widthCssPx: number,
  heightCssPx: number,
  peak: number,
  theme: WaveformTheme,
): void {
  ctx.clearRect(0, 0, widthCssPx, heightCssPx);
  if (pcm.length === 0 || widthCssPx <= 0 || heightCssPx <= 0) return;
  ctx.strokeStyle = theme.wave;
  ctx.fillStyle = theme.wave;
  ctx.lineWidth = 1;
  const spp = samplesPerPixel(mapping);
  if (spp >= POLYLINE_SAMPLES_PER_PIXEL) {
    drawMinMax(ctx, pcm, mapping, widthCssPx, heightCssPx, peak);
  } else {
    drawPolyline(ctx, pcm, mapping, heightCssPx, peak, spp < POINT_SAMPLES_PER_PIXEL);
  }
}

export type OverlayState = {
  cursorSample: number;
  selection: { startSample: number; endSample: number } | null;
  playheadSample: number | null;
  hoverSample: number | null;
  hoverEdge: "start" | "end" | null;
};

export function drawOverlay(
  ctx: CanvasRenderingContext2D,
  mapping: ViewMapping,
  widthCssPx: number,
  heightCssPx: number,
  overlay: OverlayState,
  theme: WaveformTheme,
): void {
  ctx.clearRect(0, 0, widthCssPx, heightCssPx);
  if (overlay.playheadSample !== null) {
    const x = xFromSample(overlay.playheadSample, mapping);
    if (x > 0) {
      ctx.fillStyle = theme.progress;
      ctx.fillRect(0, 0, Math.min(widthCssPx, Math.max(0, x)), heightCssPx);
    }
  }
  if (overlay.selection) {
    const x0 = xFromSample(overlay.selection.startSample, mapping);
    const x1 = xFromSample(overlay.selection.endSample, mapping);
    const left = Math.min(x0, x1);
    const width = Math.max(1, Math.abs(x1 - x0));
    ctx.fillStyle = theme.selectionFill;
    ctx.fillRect(left, 0, width, heightCssPx);
    ctx.strokeStyle = theme.selectionEdge;
    ctx.lineWidth = overlay.hoverEdge ? 2 : 1;
    ctx.beginPath();
    ctx.moveTo(x0 + 0.5, 0);
    ctx.lineTo(x0 + 0.5, heightCssPx);
    ctx.moveTo(x1 + 0.5, 0);
    ctx.lineTo(x1 + 0.5, heightCssPx);
    ctx.stroke();
  } else {
    const x = xFromSample(overlay.cursorSample, mapping);
    ctx.strokeStyle = theme.cursor;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, heightCssPx);
    ctx.stroke();
  }
  if (overlay.playheadSample !== null) {
    const x = xFromSample(overlay.playheadSample, mapping);
    ctx.strokeStyle = theme.playhead;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x + 0.5, 0);
    ctx.lineTo(x + 0.5, heightCssPx);
    ctx.stroke();
  }
}

function hslColor(spaceSeparated: string, alpha: number): string {
  const parts = spaceSeparated.trim().split(/[\s/]+/);
  if (parts.length < 3) return `rgba(23,23,23,${alpha})`;
  return `hsla(${parts[0]}, ${parts[1]}, ${parts[2]}, ${alpha})`;
}

export function readWaveformTheme(dark: boolean): WaveformTheme {
  const primary =
    typeof document !== "undefined"
      ? getComputedStyle(document.documentElement).getPropertyValue("--primary")
      : dark
        ? "0 0% 98%"
        : "240 5.9% 10%";
  return {
    wave: dark ? "#454545" : "#cfcfcf",
    selectionFill: hslColor(primary, 0.12),
    selectionEdge: dark ? "#737373" : "#a3a3a3",
    cursor: dark ? "#f5f5f5" : "#171717",
    playhead: dark ? "#d4d4d4" : "#404040",
    progress: dark ? "rgba(154,154,154,0.18)" : "rgba(107,107,107,0.14)",
  };
}
