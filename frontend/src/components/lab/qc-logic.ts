// Pure QC / Dataset-Health logic. Ported from the legacy frontend's
// src/qc/qcLogic.ts and adapted to the live GET /api/dataset-runs/{id}/qc
// clip shape. The two scores (transcript_match, speaker_check) are INDEPENDENT
// necessary gates (AND) — never blended. A null score means "unscored" and is
// treated as rejected, matching the backend (_threshold_status in dataset_qc.py).

export type QcClip = {
  clipId: string;
  audioUrl?: string | null;
  durationSec: number;
  transcriptMatch: number | null;
  speakerCheck: number | null;
  reasonCodes: string[];
  trainingText: string;
};

export type HumanLabeledClip = {
  clipId: string;
  status: "accepted" | "rejected";
  transcriptMatch: number | null;
  speakerCheck: number | null;
  cleanAccepted: boolean;
};

export type ManualOverride = "force_keep" | "force_reject";
export type ThresholdStatus = "accepted" | "rejected";

export type ClipWithMargins = {
  clip: QcClip;
  transcriptMargin: number;
  speakerMargin: number;
  riskMargin: number;
  transcriptGap: number;
  speakerGap: number;
  recoveryGap: number;
};

export type CurvePoint = {
  threshold: number;
  acceptedDurationSec: number;
  acceptedClipCount: number;
};

export type CombinedSummary = {
  acceptedCount: number;
  rejectedCount: number;
  acceptedDurationSec: number;
  rejectedDurationSec: number;
};

export type KeptSort = "risk" | "transcript" | "speaker";

const NEG_INF = Number.NEGATIVE_INFINITY;

function scoreOr(score: number | null | undefined): number {
  return typeof score === "number" && Number.isFinite(score) ? score : NEG_INF;
}

function effectiveOverride(
  clip: QcClip,
  overrides: Record<string, ManualOverride | null | undefined>,
): ManualOverride | null {
  return overrides[clip.clipId] ?? null;
}

export function thresholdStatus(
  clip: QcClip,
  transcriptThreshold: number,
  speakerThreshold: number,
): ThresholdStatus {
  if (scoreOr(clip.transcriptMatch) < transcriptThreshold) return "rejected";
  if (scoreOr(clip.speakerCheck) < speakerThreshold) return "rejected";
  return "accepted";
}

export function finalStatus(
  clip: QcClip,
  transcriptThreshold: number,
  speakerThreshold: number,
  overrides: Record<string, ManualOverride | null | undefined> = {},
): ThresholdStatus {
  const override = effectiveOverride(clip, overrides);
  if (override === "force_keep") return "accepted";
  if (override === "force_reject") return "rejected";
  return thresholdStatus(clip, transcriptThreshold, speakerThreshold);
}

export function margins(
  clip: QcClip,
  transcriptThreshold: number,
  speakerThreshold: number,
): ClipWithMargins {
  const t = scoreOr(clip.transcriptMatch);
  const s = scoreOr(clip.speakerCheck);
  const transcriptMargin = t - transcriptThreshold;
  const speakerMargin = s - speakerThreshold;
  const transcriptGap = Math.max(0, transcriptThreshold - t);
  const speakerGap = Math.max(0, speakerThreshold - s);
  return {
    clip,
    transcriptMargin,
    speakerMargin,
    riskMargin: Math.min(transcriptMargin, speakerMargin),
    transcriptGap,
    speakerGap,
    recoveryGap: Math.max(transcriptGap, speakerGap),
  };
}

function byClipId(a: QcClip, b: QcClip): number {
  return a.clipId.localeCompare(b.clipId);
}

function withMarginsByStatus(
  clips: QcClip[],
  transcriptThreshold: number,
  speakerThreshold: number,
  status: ThresholdStatus,
  overrides: Record<string, ManualOverride | null | undefined>,
): ClipWithMargins[] {
  return clips
    .filter((c) => finalStatus(c, transcriptThreshold, speakerThreshold, overrides) === status)
    .map((c) => margins(c, transcriptThreshold, speakerThreshold));
}

/** Accepted clips ordered so the riskiest kept (closest to failing) come first. */
export function riskiestKept(
  clips: QcClip[],
  transcriptThreshold: number,
  speakerThreshold: number,
  sort: KeptSort = "risk",
  overrides: Record<string, ManualOverride | null | undefined> = {},
): ClipWithMargins[] {
  const kept = withMarginsByStatus(clips, transcriptThreshold, speakerThreshold, "accepted", overrides);
  kept.sort((l, r) => {
    if (sort === "transcript") return l.transcriptMargin - r.transcriptMargin || byClipId(l.clip, r.clip);
    if (sort === "speaker") return l.speakerMargin - r.speakerMargin || byClipId(l.clip, r.clip);
    return l.riskMargin - r.riskMargin || byClipId(l.clip, r.clip);
  });
  return kept;
}

/** Select up to ten accepted clips, spread across their source order. */
export function evenlyDistributedKept(
  clips: QcClip[],
  transcriptThreshold: number,
  speakerThreshold: number,
  overrides: Record<string, ManualOverride | null | undefined> = {},
  random: () => number = Math.random,
): ClipWithMargins[] {
  const kept = withMarginsByStatus(clips, transcriptThreshold, speakerThreshold, "accepted", overrides);
  const sampleSize = Math.min(10, kept.length);
  if (sampleSize === 0) return [];
  if (sampleSize === kept.length) return kept;

  return Array.from({ length: sampleSize }, (_, index) => {
    const start = Math.floor((index * kept.length) / sampleSize);
    const end = Math.floor(((index + 1) * kept.length) / sampleSize);
    const rangeSize = Math.max(1, end - start);
    const randomIndex = Math.min(rangeSize - 1, Math.floor(random() * rangeSize));
    return kept[start + randomIndex];
  });
}

export function clampScore(score: number | null | undefined): number | null {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  return Math.max(0, Math.min(100, score));
}

/**
 * Yield curve at every threshold that can change the accepted set. Scores are
 * preserved so the threshold handle can snap to the actual clip boundaries.
 */
export function thresholdImpactCurve(
  clips: QcClip[],
  getScore: (clip: QcClip) => number | null | undefined,
): CurvePoint[] {
  const events = new Map<number, { duration: number; count: number }>();
  for (const clip of clips) {
    const score = clampScore(getScore(clip));
    if (score === null) continue;
    const event = events.get(score) ?? { duration: 0, count: 0 };
    event.duration += clip.durationSec;
    event.count += 1;
    events.set(score, event);
  }
  const thresholds = Array.from(new Set([0, 100, ...events.keys()])).sort((a, b) => a - b);
  const curve: CurvePoint[] = new Array(thresholds.length);
  let acceptedDurationSec = 0;
  let acceptedClipCount = 0;
  for (let index = thresholds.length - 1; index >= 0; index -= 1) {
    const threshold = thresholds[index];
    const event = events.get(threshold);
    acceptedDurationSec += event?.duration ?? 0;
    acceptedClipCount += event?.count ?? 0;
    curve[index] = {
      threshold,
      acceptedDurationSec: Number(acceptedDurationSec.toFixed(6)),
      acceptedClipCount,
    };
  }
  return curve;
}

export type HistogramBin = {
  /** Inclusive lower edge of the bin, 0..100. */
  start: number;
  /** Exclusive upper edge (except the final bin, which is inclusive of 100). */
  end: number;
  /** Bin center, used as the chart X value. */
  center: number;
  count: number;
};

/** Bin scores into `binCount` equal-width buckets across 0..100. Nulls dropped. */
export function histogram(
  clips: QcClip[],
  getScore: (clip: QcClip) => number | null | undefined,
  binCount = 20,
): HistogramBin[] {
  const width = 100 / binCount;
  const bins: HistogramBin[] = Array.from({ length: binCount }, (_, i) => ({
    start: i * width,
    end: (i + 1) * width,
    center: i * width + width / 2,
    count: 0,
  }));
  for (const clip of clips) {
    const score = getScore(clip);
    if (typeof score !== "number" || !Number.isFinite(score)) continue;
    const clamped = Math.max(0, Math.min(100, score));
    let index = Math.floor(clamped / width);
    if (index >= binCount) index = binCount - 1;
    bins[index].count += 1;
  }
  return bins;
}

/** Count of clips whose score is null/unscored for a given field. */
export function unscoredCount(
  clips: QcClip[],
  getScore: (clip: QcClip) => number | null | undefined,
): number {
  let n = 0;
  for (const clip of clips) {
    const score = getScore(clip);
    if (typeof score !== "number" || !Number.isFinite(score)) n += 1;
  }
  return n;
}

export function combinedSummary(
  clips: QcClip[],
  transcriptThreshold: number,
  speakerThreshold: number,
  overrides: Record<string, ManualOverride | null | undefined> = {},
): CombinedSummary {
  const summary: CombinedSummary = {
    acceptedCount: 0,
    rejectedCount: 0,
    acceptedDurationSec: 0,
    rejectedDurationSec: 0,
  };
  for (const clip of clips) {
    if (finalStatus(clip, transcriptThreshold, speakerThreshold, overrides) === "accepted") {
      summary.acceptedCount += 1;
      summary.acceptedDurationSec += clip.durationSec;
    } else {
      summary.rejectedCount += 1;
      summary.rejectedDurationSec += clip.durationSec;
    }
  }
  summary.acceptedDurationSec = Number(summary.acceptedDurationSec.toFixed(6));
  summary.rejectedDurationSec = Number(summary.rejectedDurationSec.toFixed(6));
  return summary;
}
