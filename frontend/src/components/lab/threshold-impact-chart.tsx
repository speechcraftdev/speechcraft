"use client";

import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Scatter,
  XAxis,
  YAxis,
} from "recharts";
import type { CurvePoint } from "./qc-logic";

type ThresholdImpactChartProps = {
  title: string;
  points: CurvePoint[];
  threshold: number;
  onThresholdChange: (value: number) => void;
  humanLabels?: HumanLabelMarker[];
  height?: number;
};

export type HumanLabelMarker = {
  clipId: string;
  score: number;
  status: "accepted" | "rejected";
  cleanAccepted: boolean;
};

const Y_AXIS_WIDTH = 44;
const PLOT_MARGIN = { top: 6, right: 8, bottom: 0, left: 0 };
const X_AXIS_HEIGHT = 30;

/** Same padding the pre-revamp card used so a shallow curve still fills the plot. */
export function durationDomain(points: Array<{ acceptedDurationSec: number | null }>): { min: number; max: number } {
  const values = points
    .map((point) => point.acceptedDurationSec)
    .filter((value): value is number => Number.isFinite(value));
  if (values.length === 0) return { min: 0, max: 60 };
  const min = Math.min(...values);
  const max = Math.max(...values);
  if (max <= min) {
    const pad = Math.max(1, max * 0.05);
    return { min: Math.max(0, min - pad), max: max + pad };
  }
  const pad = Math.max(1, (max - min) * 0.08);
  return { min: Math.max(0, min - pad), max: max + pad };
}

export function formatDurationCompact(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return "0s";
  if (totalSeconds >= 3600) return `${(totalSeconds / 3600).toFixed(1)}h`;
  if (totalSeconds >= 60) return `${(totalSeconds / 60).toFixed(1)}m`;
  return `${Math.round(totalSeconds)}s`;
}

function formatDurationHms(totalSeconds: number): string {
  const roundedSeconds = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(roundedSeconds / 3600);
  const minutes = Math.floor((roundedSeconds % 3600) / 60);
  const seconds = roundedSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

export function ThresholdImpactChart({
  title,
  points,
  threshold,
  onThresholdChange,
  humanLabels = [],
  height = 240,
}: ThresholdImpactChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const sliderRef = useRef<HTMLDivElement>(null);
  const hoverMetricsRef = useRef<HTMLDivElement>(null);
  const hoverThresholdRef = useRef<HTMLSpanElement>(null);
  const hoverDurationRef = useRef<HTMLSpanElement>(null);
  const hoverCircleRef = useRef<HTMLDivElement>(null);
  const axisThresholdRef = useRef<HTMLSpanElement>(null);
  const axisDurationRef = useRef<HTMLSpanElement>(null);
  const dragValueRef = useRef(threshold);
  const linePoints = useMemo(
    () =>
      points.map((point) => ({
        ...point,
        acceptedDurationSec: point.acceptedClipCount > 0 ? point.acceptedDurationSec : null,
      })),
    [points],
  );
  const yDomain = useMemo(() => durationDomain(linePoints), [linePoints]);
  const markerPoints = useMemo(() => {
    const range = Math.max(1, yDomain.max - yDomain.min);
    const slots = new Map<string, number>();
    return humanLabels.map((marker) => {
      const key = `${marker.score}:${marker.cleanAccepted ? "clean" : "other"}`;
      const slot = slots.get(key) ?? 0;
      slots.set(key, slot + 1);
      return {
        ...marker,
        kind: "human-marker" as const,
        x: marker.score,
        threshold: marker.score,
        y: yDomain.min + range * (0.04 + (slot % 4) * 0.025),
      };
    });
  }, [humanLabels, yDomain]);
  const meaningfulThresholds = useMemo(
    () => points.map((point) => point.threshold).sort((a, b) => a - b),
    [points],
  );
  const current = points.find((point) => point.threshold >= threshold) ?? points.at(-1);
  const nothingLeft = !current || current.acceptedClipCount === 0;
  const plotLeft = PLOT_MARGIN.left + Y_AXIS_WIDTH;
  const plotRight = PLOT_MARGIN.right;

  const scoreFromClientX = useCallback((clientX: number): number => {
    const el = containerRef.current;
    if (!el) return threshold;
    const rect = el.getBoundingClientRect();
    const left = rect.left + plotLeft;
    const plotWidth = rect.width - plotLeft - plotRight;
    if (plotWidth <= 0) return threshold;
    const ratio = (clientX - left) / plotWidth;
    const clamped = Math.max(0, Math.min(1, ratio));
    const rawScore = clamped * 100;
    return meaningfulThresholds.reduce((nearest, candidate) =>
      Math.abs(candidate - rawScore) < Math.abs(nearest - rawScore) ? candidate : nearest,
    meaningfulThresholds[0] ?? threshold);
  }, [meaningfulThresholds, threshold, plotLeft, plotRight]);

  const setSliderPosition = useCallback(
    (value: number) => {
      if (!sliderRef.current) return;
      sliderRef.current.style.left =
        `calc(${plotLeft}px + (100% - ${plotLeft + plotRight}px) * ${value / 100})`;
    },
    [plotLeft, plotRight],
  );

  const updateHoverAtScore = useCallback(
    (score: number, showCircle: boolean) => {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const plotWidth = rect.width - plotLeft - plotRight;
      const plotHeight = height - PLOT_MARGIN.top - X_AXIS_HEIGHT - PLOT_MARGIN.bottom;
      if (plotWidth <= 0 || plotHeight <= 0) return;

      const clampedScore = Math.max(0, Math.min(100, score));
      const ratio = clampedScore / 100;
      const curvePoint = points.find((point) => point.threshold >= clampedScore) ?? points.at(-1);
      if (!curvePoint) return;
      const duration = curvePoint.acceptedDurationSec;
      const yRatio = (duration - yDomain.min) / (yDomain.max - yDomain.min || 1);
      const circle = hoverCircleRef.current;
      if (circle) {
        circle.style.left = `${plotLeft + ratio * plotWidth}px`;
        circle.style.top = `${PLOT_MARGIN.top + (1 - yRatio) * plotHeight}px`;
        circle.classList.toggle("invisible", !showCircle);
      }
      if (hoverThresholdRef.current) {
        hoverThresholdRef.current.textContent = `Threshold: ${clampedScore.toFixed(2)}`;
      }
      if (hoverDurationRef.current) {
        hoverDurationRef.current.textContent = `Accepted Duration: ${formatDurationHms(duration)}`;
      }
      hoverMetricsRef.current?.classList.remove("invisible");
    },
    [height, linePoints, plotLeft, plotRight, points, yDomain],
  );

  const updateAxisLabels = useCallback(
    (value: number, visible: boolean) => {
      const el = containerRef.current;
      const curvePoint = points.find((point) => point.threshold >= value) ?? points.at(-1);
      if (!el || !curvePoint) return;
      const rect = el.getBoundingClientRect();
      const plotWidth = rect.width - plotLeft - plotRight;
      const plotHeight = height - PLOT_MARGIN.top - X_AXIS_HEIGHT - PLOT_MARGIN.bottom;
      if (plotWidth <= 0 || plotHeight <= 0) return;

      const clampedValue = Math.max(0, Math.min(100, value));
      const yRatio = (curvePoint.acceptedDurationSec - yDomain.min) / (yDomain.max - yDomain.min || 1);
      if (axisThresholdRef.current) {
        axisThresholdRef.current.textContent = clampedValue.toFixed(2);
        axisThresholdRef.current.style.left = `${plotLeft + (clampedValue / 100) * plotWidth}px`;
        axisThresholdRef.current.classList.toggle("invisible", !visible);
      }
      if (axisDurationRef.current) {
        axisDurationRef.current.textContent = formatDurationCompact(curvePoint.acceptedDurationSec);
        axisDurationRef.current.style.top = `${PLOT_MARGIN.top + (1 - yRatio) * plotHeight}px`;
        axisDurationRef.current.classList.toggle("invisible", !visible);
      }
    },
    [height, plotLeft, plotRight, points, yDomain],
  );

  useEffect(() => {
    updateAxisLabels(threshold, true);
  }, [threshold, updateAxisLabels]);

  const updateHover = useCallback(
    (clientX: number) => {
      const el = containerRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const plotWidth = rect.width - plotLeft - plotRight;
      if (plotWidth <= 0) return;
      const score = Math.max(0, Math.min(1, (clientX - rect.left - plotLeft) / plotWidth)) * 100;
      updateHoverAtScore(score, true);
    },
    [plotLeft, plotRight, updateHoverAtScore],
  );

  const hideHover = useCallback(() => {
    hoverMetricsRef.current?.classList.add("invisible");
    hoverCircleRef.current?.classList.add("invisible");
  }, []);

  const handlePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const initialValue = scoreFromClientX(event.clientX);
      dragValueRef.current = initialValue;
      setSliderPosition(initialValue);
      updateAxisLabels(initialValue, false);
      hoverCircleRef.current?.classList.add("invisible");
      updateHoverAtScore(initialValue, false);
      const onMove = (moveEvent: PointerEvent) => {
        const nextValue = scoreFromClientX(moveEvent.clientX);
        dragValueRef.current = nextValue;
        setSliderPosition(nextValue);
        updateAxisLabels(nextValue, false);
        hoverCircleRef.current?.classList.add("invisible");
        updateHoverAtScore(nextValue, false);
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onCancel);
        onThresholdChange(dragValueRef.current);
        updateAxisLabels(dragValueRef.current, true);
        updateHoverAtScore(dragValueRef.current, true);
      };
      const onCancel = () => {
        setSliderPosition(threshold);
        updateAxisLabels(threshold, true);
        updateHoverAtScore(threshold, true);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onCancel);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onCancel);
    },
    [onThresholdChange, scoreFromClientX, setSliderPosition, threshold, updateAxisLabels, updateHoverAtScore],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        const previous = [...meaningfulThresholds].reverse().find((value) => value < threshold);
        const nextThreshold = previous ?? meaningfulThresholds[0] ?? 0;
        updateAxisLabels(nextThreshold, true);
        onThresholdChange(nextThreshold);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        const next = meaningfulThresholds.find((value) => value > threshold);
        const nextThreshold = next ?? meaningfulThresholds.at(-1) ?? 100;
        updateAxisLabels(nextThreshold, true);
        onThresholdChange(nextThreshold);
      }
    },
    [meaningfulThresholds, onThresholdChange, threshold, updateAxisLabels],
  );

  return (
    <div className="border border-border p-4">
      <div className="mb-1 flex items-baseline justify-between gap-4">
        <h3 className="font-serif text-lg leading-none">{title}</h3>
        <div
          ref={hoverMetricsRef}
          className="invisible flex h-10 shrink-0 flex-col items-end text-xs leading-5 tabular-nums text-white"
        >
          <span ref={hoverThresholdRef}>Threshold: 00.00</span>
          <span ref={hoverDurationRef}>Accepted Duration: 0s</span>
        </div>
      </div>
      {humanLabels.length > 0 ? (
        <div className="mb-2 flex items-center gap-3 text-[11px] text-muted-foreground">
          <span className="inline-flex items-center gap-1">
            <span className="size-2 rounded-full bg-emerald-500" /> Accepted · clean
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="size-2 rounded-full bg-red-500" /> Rejected or edited
          </span>
        </div>
      ) : null}
      {nothingLeft ? (
        <p className="mt-1 text-xs text-[#878787]">Nothing remains above this</p>
      ) : null}
      <p className="mb-2 mt-3 text-xs text-[#878787]">Accepted duration</p>

      <div
        ref={containerRef}
        className="relative touch-none select-none"
        style={{ height }}
        onPointerDown={handlePointerDown}
        onMouseMove={(event) => updateHover(event.clientX)}
        onMouseLeave={hideHover}
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart
            data={linePoints}
            margin={PLOT_MARGIN}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid-stroke, #e6e6e6)" vertical={false} />
            <XAxis
              dataKey="threshold"
              type="number"
              domain={[0, 100]}
              axisLine={false}
              tickLine={false}
              ticks={[0, 25, 50, 75, 100]}
              tick={{ fill: "#878787", fontSize: 10 }}
            />
            <YAxis
              width={Y_AXIS_WIDTH}
              domain={[yDomain.min, yDomain.max]}
              axisLine={false}
              tickLine={false}
              tickFormatter={formatDurationCompact}
              tick={{ fill: "#878787", fontSize: 10 }}
            />
            <Line
              type="stepAfter"
              dataKey="acceptedDurationSec"
              stroke="var(--chart-bar-fill)"
              strokeWidth={1.5}
              dot={false}
              activeDot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            {markerPoints.length > 0 ? (
              <Scatter
                data={markerPoints}
                dataKey="y"
                fill="currentColor"
                shape={(props) => {
                  const marker = props.payload as (typeof markerPoints)[number];
                  return (
                    <circle
                      cx={props.cx}
                      cy={props.cy}
                      r={4}
                      fill={marker.cleanAccepted ? "#22c55e" : "#ef4444"}
                      stroke="var(--background)"
                      strokeWidth={1}
                    />
                  );
                }}
                isAnimationActive={false}
              />
            ) : null}
          </LineChart>
        </ResponsiveContainer>

        <span
          ref={axisThresholdRef}
          className="pointer-events-none invisible absolute bottom-0 -translate-x-1/2 translate-y-full bg-background px-0.5 text-[10px] tabular-nums text-[#878787]"
          aria-hidden="true"
        >
          {threshold.toFixed(2)}
        </span>
        <span
          ref={axisDurationRef}
          className="pointer-events-none invisible absolute left-0 -translate-x-full -translate-y-1/2 bg-background pr-1 text-[10px] tabular-nums text-[#878787]"
          aria-hidden="true"
        >
          {formatDurationCompact(current?.acceptedDurationSec ?? 0)}
        </span>

        <div
          ref={hoverCircleRef}
          className="invisible pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-background bg-foreground"
          aria-hidden="true"
        />

        <div
          ref={sliderRef}
          role="slider"
          aria-label={`${title} minimum threshold`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={threshold}
          tabIndex={0}
          onKeyDown={handleKeyDown}
          className="absolute top-0 flex h-full w-4 -translate-x-1/2 cursor-ew-resize items-start justify-center outline-none"
          style={{
            left: `calc(${plotLeft}px + (100% - ${plotLeft + plotRight}px) * ${threshold / 100})`,
          }}
        >
          <div className="pointer-events-none absolute inset-y-0 w-px bg-foreground" />
          <div className="pointer-events-none relative z-10 mt-[-4px] h-2.5 w-2.5 rotate-45 border border-foreground bg-background" />
        </div>
      </div>
    </div>
  );
}
