import { describe, expect, test } from "bun:test";
import {
  clipLabDurationSamples,
  createMockClips,
  DEFAULT_SORT_DIRECTIONS,
  initialReviewClipId,
  sortClips,
  type LabClip,
  type SortMode,
} from "./lab-data";

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

function makeSortClips(): LabClip[] {
  const base = createMockClips(1)[0]!;
  const make = (
    id: string,
    order: number,
    tc: number | null,
    sp: number | null,
    duration: number | null,
    rq: number | null,
  ): LabClip => ({
    ...base,
    id,
    order,
    transcriptMatchRaw: tc,
    speakerCheckRaw: sp,
    effectiveDurationRaw: duration,
    referenceQuality: rq,
  });

  return [
    make("third", 3, 50, 40, 8, 20),
    make("missing", 5, null, null, null, null),
    make("zero", 4, 0, 0, 0, 0),
    make("first", 1, 90, 80, 12, 70),
    make("second", 2, 50, 40, 8, 20),
  ];
}

const ids = (clips: LabClip[]) => clips.map((clip) => clip.id);

describe("sortClips direction", () => {
  test("all methods default to downward direction", () => {
    expect(DEFAULT_SORT_DIRECTIONS).toEqual({
      source: "down",
      transcript_confidence: "down",
      speaker_purity: "down",
      duration: "down",
      reference_quality: "down",
    });
  });

  test("Source Order moves from original to last and reverses", () => {
    const clips = makeSortClips();
    expect(ids(sortClips(clips, "source"))).toEqual(["first", "second", "third", "zero", "missing"]);
    expect(ids(sortClips(clips, "source", "down"))).toEqual(["first", "second", "third", "zero", "missing"]);
    expect(ids(sortClips(clips, "source", "up"))).toEqual(["missing", "zero", "third", "second", "first"]);
  });

  test.each([
    ["transcript_confidence", ["first", "second", "third", "zero", "missing"], ["zero", "second", "third", "first", "missing"]],
    ["speaker_purity", ["first", "second", "third", "zero", "missing"], ["zero", "second", "third", "first", "missing"]],
    ["duration", ["first", "second", "third", "zero", "missing"], ["zero", "second", "third", "first", "missing"]],
    ["reference_quality", ["first", "second", "third", "zero", "missing"], ["zero", "second", "third", "first", "missing"]],
  ] as Array<[Exclude<SortMode, "source">, string[], string[]]>) (
    "%s sorts in both directions, keeps zero valid, null last, and ties in source order",
    (mode, down, up) => {
      const clips = makeSortClips();
      expect(ids(sortClips(clips, mode, "down"))).toEqual(down);
      expect(ids(sortClips(clips, mode, "up"))).toEqual(up);
    },
  );

  test("does not mutate the input array and defaults to the prior descending metric order", () => {
    const clips = makeSortClips();
    const original = [...clips];
    expect(ids(sortClips(clips, "reference_quality"))).toEqual(ids(sortClips(clips, "reference_quality", "down")));
    expect(clips).toEqual(original);
  });
});
