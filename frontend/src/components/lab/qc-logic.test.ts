import { describe, expect, test } from "bun:test";
import {
  combinedSummary,
  evenlyDistributedKept,
  histogram,
  riskiestKept,
  thresholdImpactCurve,
  thresholdStatus,
  unscoredCount,
  type QcClip,
} from "./qc-logic";

function clip(
  clipId: string,
  transcriptMatch: number | null,
  speakerCheck: number | null,
  durationSec = 2,
): QcClip {
  return { clipId, transcriptMatch, speakerCheck, durationSec, reasonCodes: [], trainingText: clipId };
}

// transcript threshold 85, speaker threshold 70 (defaults)
const T = 85;
const S = 70;

const clips: QcClip[] = [
  clip("a", 95, 90), // clear accept
  clip("b", 86, 71), // barely accepted (riskiest kept)
  clip("c", 84, 95), // rejected on transcript only (best rejected, transcript_only)
  clip("d", 90, 68), // rejected on speaker only
  clip("e", 40, 30), // clear reject
  clip("f", null, 80), // unscored transcript => rejected
];

describe("thresholdStatus (AND-gate, null = rejected)", () => {
  test("both gates required", () => {
    expect(thresholdStatus(clip("x", 95, 90), T, S)).toBe("accepted");
    expect(thresholdStatus(clip("x", 84, 90), T, S)).toBe("rejected");
    expect(thresholdStatus(clip("x", 95, 69), T, S)).toBe("rejected");
  });
  test("null score is rejected", () => {
    expect(thresholdStatus(clip("x", null, 90), T, S)).toBe("rejected");
    expect(thresholdStatus(clip("x", 95, null), T, S)).toBe("rejected");
  });
});

describe("combinedSummary", () => {
  test("counts accepted vs rejected", () => {
    const s = combinedSummary(clips, T, S);
    expect(s.acceptedCount).toBe(2); // a, b
    expect(s.rejectedCount).toBe(4); // c, d, e, f
    expect(s.acceptedDurationSec).toBe(4);
  });
});

describe("riskiestKept", () => {
  test("accepted clips ordered by smallest risk margin first", () => {
    const kept = riskiestKept(clips, T, S, "risk");
    expect(kept.map((k) => k.clip.clipId)).toEqual(["b", "a"]); // b is closest to failing
  });
});

describe("evenlyDistributedKept", () => {
  test("selects accepted clips from separate portions of the dataset", () => {
    const source = Array.from({ length: 20 }, (_, index) => clip(String(index), 95, 90));
    const sampled = evenlyDistributedKept(source, T, S, {}, () => 0);
    expect(sampled.map((row) => row.clip.clipId)).toEqual([
      "0", "2", "4", "6", "8", "10", "12", "14", "16", "18",
    ]);
  });

  test("returns all accepted clips when fewer than ten qualify", () => {
    const sampled = evenlyDistributedKept(clips, T, S, {}, () => 0);
    expect(sampled.map((row) => row.clip.clipId)).toEqual(["a", "b"]);
  });

  test("never includes rejected clips and caps the sample at ten", () => {
    const source = Array.from({ length: 30 }, (_, index) => clip(String(index), 95, 90));
    source[5] = clip("rejected", 20, 20);
    const sampled = evenlyDistributedKept(source, T, S, {}, () => 0.99);
    expect(sampled).toHaveLength(10);
    expect(sampled.every((row) => row.clip.clipId !== "rejected")).toBe(true);
  });
});

describe("thresholdImpactCurve", () => {
  test("accepted count is monotonic non-increasing as threshold rises", () => {
    const curve = thresholdImpactCurve(clips, (c) => c.transcriptMatch);
    expect(curve[0].acceptedClipCount).toBeGreaterThanOrEqual(curve.at(-1)!.acceptedClipCount);
    // at threshold 0, every scored clip counts (5 of 6; f is null)
    expect(curve[0].acceptedClipCount).toBe(5);
  });

  test("preserves exact score boundaries", () => {
    const curve = thresholdImpactCurve(clips, (c) => c.transcriptMatch);
    expect(curve.map((point) => point.threshold)).toEqual([0, 40, 84, 86, 90, 95, 100]);
    expect(curve.find((point) => point.threshold === 84)?.acceptedClipCount).toBe(4);
    expect(curve.find((point) => point.threshold === 86)?.acceptedClipCount).toBe(3);
  });
});

describe("histogram", () => {
  test("bins scored clips, drops nulls", () => {
    const bins = histogram(clips, (c) => c.transcriptMatch, 10);
    const total = bins.reduce((n, b) => n + b.count, 0);
    expect(total).toBe(5); // f (null) excluded
    expect(unscoredCount(clips, (c) => c.transcriptMatch)).toBe(1);
  });
});
