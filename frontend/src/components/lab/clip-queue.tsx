"use client";

import { Button } from "@midday/ui/button";
import { cn } from "@midday/ui/cn";
import { Icons } from "@midday/ui/icons";
import { Input } from "@midday/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@midday/ui/popover";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useRef, useState } from "react";
import {
  type LabClip,
  type MachineBucket,
  MACHINE_BUCKET_LABELS,
  MACHINE_BUCKET_ORDER,
  type ReviewStatus,
  REVIEW_STATUS_ORDER,
  type SortMode,
  type SortDirections,
  SORT_OPTIONS,
  STATUS_LABELS,
  formatSeconds,
} from "./lab-data";
import { StatusBadge } from "./status-badge";

type ClipQueueProps = {
  clips: LabClip[];
  activeClipId: string | null;
  search: string;
  onSearchChange: (value: string) => void;
  statuses: ReviewStatus[];
  buckets: MachineBucket[];
  onToggleStatus: (status: ReviewStatus) => void;
  onToggleBucket: (bucket: MachineBucket) => void;
  onClearFilters: () => void;
  sortMode: SortMode;
  onSortModeChange: (mode: SortMode) => void;
  sortDirections: SortDirections;
  onToggleSortDirection: (mode: SortMode) => void;
  onSelect: (id: string) => void;
};

export function ClipQueue({
  clips,
  activeClipId,
  search,
  onSearchChange,
  statuses,
  buckets,
  onToggleStatus,
  onToggleBucket,
  onClearFilters,
  sortMode,
  onSortModeChange,
  sortDirections,
  onToggleSortDirection,
  onSelect,
}: ClipQueueProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [sortOpen, setSortOpen] = useState(false);
  const activeFilterCount = statuses.length + buckets.length;

  const virtualizer = useVirtualizer({
    count: clips.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 104,
    overscan: 8,
  });

  useEffect(() => {
    if (!activeClipId) return;
    const activeIndex = clips.findIndex((clip) => clip.id === activeClipId);
    if (activeIndex < 0) return;
    virtualizer.scrollToIndex(activeIndex, { align: "auto" });
  }, [activeClipId, clips, virtualizer]);

  const filterSummary =
    activeFilterCount > 0
      ? [
          ...statuses.map((s) => STATUS_LABELS[s]),
          ...buckets.map((b) => MACHINE_BUCKET_LABELS[b]),
        ].join(", ")
      : "All slices";

  return (
    <aside className="flex h-full w-[320px] flex-shrink-0 flex-col border-r border-border">
      {/* Tools: search + filter */}
      <div className="flex items-center gap-2 p-3">
        <div className="relative flex-1">
          <Icons.Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            aria-label="Search clips"
            placeholder="Search clips"
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            className="h-9 pl-8"
          />
        </div>

        <Popover>
          <PopoverTrigger asChild>
            <Button variant="outline" size="sm" className="h-9 gap-1.5">
              <Icons.Filter className="size-4" />
              {activeFilterCount > 0 ? `Filters (${activeFilterCount})` : "Filter"}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-64 p-0">
            <div className="p-3">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Status
              </p>
              <div className="flex flex-wrap gap-1.5">
                {REVIEW_STATUS_ORDER.map((status) => (
                  <button
                    key={status}
                    type="button"
                    onClick={() => onToggleStatus(status)}
                    className={cn(
                      "border px-2 py-1 text-xs transition-colors",
                      statuses.includes(status)
                        ? "border-primary text-primary"
                        : "border-border text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {STATUS_LABELS[status]}
                  </button>
                ))}
              </div>

              <p className="mb-2 mt-4 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Statistics
              </p>
              <div className="flex flex-wrap gap-1.5">
                {MACHINE_BUCKET_ORDER.map((bucket) => (
                  <button
                    key={bucket}
                    type="button"
                    onClick={() => onToggleBucket(bucket)}
                    className={cn(
                      "border px-2 py-1 text-xs transition-colors",
                      buckets.includes(bucket)
                        ? "border-primary text-primary"
                        : "border-border text-muted-foreground hover:text-foreground",
                    )}
                  >
                    {MACHINE_BUCKET_LABELS[bucket]}
                  </button>
                ))}
              </div>

              {activeFilterCount > 0 ? (
                <button
                  type="button"
                  onClick={onClearFilters}
                  className="mt-4 text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
                >
                  Clear filters
                </button>
              ) : null}
            </div>
          </PopoverContent>
        </Popover>
      </div>

      {/* Sort */}
      <div className="px-3 pb-2">
        <Popover open={sortOpen} onOpenChange={setSortOpen}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-9 w-full justify-between font-normal"
              aria-label={`Sort by ${SORT_OPTIONS.find((option) => option.value === sortMode)?.label ?? "Source Order"}`}
            >
              <span>{SORT_OPTIONS.find((option) => option.value === sortMode)?.label ?? "Source Order"}</span>
              <Icons.ChevronDown className="size-4 text-muted-foreground" />
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-[294px] p-1">
            {SORT_OPTIONS.map((option) => (
              <div key={option.value} className="flex items-center gap-1">
                <button
                  type="button"
                  aria-pressed={sortMode === option.value}
                  onClick={() => {
                    onSortModeChange(option.value);
                    setSortOpen(false);
                  }}
                  className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-sm px-2 text-left text-sm hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {sortMode === option.value ? (
                    <Icons.Check aria-hidden="true" className="size-4 shrink-0" />
                  ) : (
                    <span aria-hidden="true" className="size-4 shrink-0" />
                  )}
                  <span className="truncate">{option.label}</span>
                </button>
                <button
                  type="button"
                  aria-label={`Reverse ${option.label} to ${sortDirections[option.value] === "down" ? "ascending" : "descending"}`}
                  onClick={() => {
                    onToggleSortDirection(option.value);
                    setSortOpen(false);
                  }}
                  className="flex size-8 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {sortDirections[option.value] === "down" ? (
                    <Icons.ArrowDownward aria-hidden="true" className="size-4" />
                  ) : (
                    <Icons.ArrowUpward aria-hidden="true" className="size-4" />
                  )}
                </button>
              </div>
            ))}
          </PopoverContent>
        </Popover>
      </div>

      {/* Virtualized list */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-3 pb-3">
        {clips.length === 0 ? (
          <p className="px-1 py-8 text-center text-sm text-muted-foreground">
            No clips match the current filters.
          </p>
        ) : (
          <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
            {virtualizer.getVirtualItems().map((virtualRow) => {
              const clip = clips[virtualRow.index]!;
              const isActive = clip.id === activeClipId;
              return (
                <div
                  key={clip.id}
                  data-index={virtualRow.index}
                  ref={virtualizer.measureElement}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${virtualRow.start}px)`,
                  }}
                >
                  <button
                    type="button"
                    onClick={() => onSelect(clip.id)}
                    className={cn(
                      "mb-2 flex w-full flex-col gap-2 border p-3 text-left transition-colors",
                      isActive
                        ? "border-primary bg-secondary/50"
                        : "border-border hover:bg-secondary/30",
                    )}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-medium tabular-nums text-muted-foreground">
                        {clip.order}.
                      </span>
                      <StatusBadge status={clip.status} />
                    </div>
                    <p className="line-clamp-3 text-sm leading-snug">{clip.transcript}</p>
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span className="tabular-nums">{formatSeconds(clip.durationSeconds)}</span>
                    </div>
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </aside>
  );
}
