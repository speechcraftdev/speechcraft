"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { CurvePoint } from "./qc-logic";

type ThresholdImpactChartProps = {
  title: string;
  points: CurvePoint[];
  threshold: number;
  onThresholdChange: (value: number) => void;
  height?: number;
};

const Y_AXIS_WIDTH = 44;
const PLOT_MARGIN = { top: 6, right: 8, bottom: 0, left: 0 };

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

type TooltipPayloadItem = {
  payload?: CurvePoint & { acceptedDurationSec: number | null };
};

function CurveTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: TooltipPayloadItem[];
}) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload;
  if (!point) return null;
  return (
    <div className="border border-border bg-background px-2.5 py-1.5 text-xs shadow-sm">
      <p className="font-medium tabular-nums">Threshold = {point.threshold}</p>
      <p className="text-[#878787]">Duration = {formatDurationHms(point.acceptedDurationSec ?? 0)}</p>
    </div>
  );
}

export function ThresholdImpactChart({
  title,
  points,
  threshold,
  onThresholdChange,
  height = 240,
}: ThresholdImpactChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const linePoints = useMemo(
    () =>
      points.map((point) => ({
        ...point,
        acceptedDurationSec: point.acceptedClipCount > 0 ? point.acceptedDurationSec : null,
      })),
    [points],
  );
  const yDomain = useMemo(() => durationDomain(linePoints), [linePoints]);
  const current = points[threshold];
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
    return Math.round(clamped * 100);
  }, [threshold, plotLeft, plotRight]);

  const handlePointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(true);
      onThresholdChange(scoreFromClientX(event.clientX));
      const onMove = (moveEvent: PointerEvent) => {
        onThresholdChange(scoreFromClientX(moveEvent.clientX));
      };
      const onUp = () => {
        setDragging(false);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [onThresholdChange, scoreFromClientX],
  );

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        onThresholdChange(Math.max(0, threshold - 1));
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        onThresholdChange(Math.min(100, threshold + 1));
      }
    },
    [onThresholdChange, threshold],
  );

  return (
    <div className="border border-border p-4">
      <div className="mb-1 flex items-baseline justify-between">
        <h3 className="font-serif text-lg leading-none">{title}</h3>
      </div>
      {nothingLeft ? (
        <p className="mt-1 text-xs text-[#878787]">Nothing remains above this</p>
      ) : null}
      <p className="mb-2 mt-3 text-xs text-[#878787]">Accepted duration</p>

      <div
        ref={containerRef}
        className="relative touch-none select-none"
        style={{ height }}
        onPointerDown={handlePointerDown}
      >
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={linePoints} margin={PLOT_MARGIN}>
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
            <Tooltip content={<CurveTooltip />} cursor={false} />
            <Line
              type="linear"
              dataKey="acceptedDurationSec"
              stroke="var(--chart-bar-fill)"
              strokeWidth={1.5}
              dot={false}
              connectNulls={false}
              isAnimationActive={false}
            />
            <ReferenceLine
              x={threshold}
              stroke="currentColor"
              strokeWidth={1.5}
              zIndex={500}
              className="text-foreground"
            />
          </LineChart>
        </ResponsiveContainer>

        <div
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
          <div
            className={`mt-[-4px] h-2.5 w-2.5 rotate-45 border border-foreground bg-background transition-transform ${
              dragging ? "scale-125" : ""
            }`}
          />
        </div>
      </div>
    </div>
  );
}
