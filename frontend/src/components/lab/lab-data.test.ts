import { describe, expect, test } from "bun:test";
import { clipLabDurationSamples } from "./lab-data";

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
