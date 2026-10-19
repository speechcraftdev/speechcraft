import { describe, expect, test } from "bun:test";
import { clipLabDurationSamples, initialReviewClipId } from "./lab-data";

describe("clipLabDurationSamples", () => {
  test("uses the authoritative sample count and never reconstructs from seconds", () => {
    expect(clipLabDurationSamples({ durationSamples: 123456 })).toBe(123456);
    expect(clipLabDurationSamples({ durationSamples: 0 })).toBe(0);
    expect(clipLabDurationSamples({ durationSamples: null })).toBeNull();
    expect(clipLabDurationSamples({})).toBeNull();
    expect(
      clipLabDurationSamples({
        durationSamples: 48000,
      }),
    ).not.toBe(Math.round(8.001 * 48000));
  });
});

describe("initialReviewClipId", () => {
  test("chooses the first unresolved clip in queue order", () => {
    expect(
      initialReviewClipId([
        { id: "accepted", status: "accepted" },
        { id: "unresolved-first", status: "unresolved" },
        { id: "unresolved-next", status: "unresolved" },
      ]),
    ).toBe("unresolved-first");
  });

  test("falls back to the first queue clip when all clips are resolved", () => {
    expect(
      initialReviewClipId([
        { id: "rejected", status: "rejected" },
        { id: "accepted", status: "accepted" },
      ]),
    ).toBe("rejected");
  });

  test("leaves an empty queue unselected", () => {
    expect(initialReviewClipId([])).toBeNull();
  });
});
