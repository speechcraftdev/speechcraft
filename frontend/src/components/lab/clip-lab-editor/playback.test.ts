import { describe, expect, test } from "bun:test";
import { buildCutPreviewPcm, playheadMediaSample, playheadSample } from "./playback";

describe("playheadSample", () => {
  test("linear transport stays in file sample space", () => {
    const transport = { kind: "linear" as const, startSample: 100, endSample: 500 };
    expect(playheadSample(transport, 0, 16000, 1)).toBe(100);
    expect(playheadSample(transport, 0.01, 16000, 1)).toBe(260);
    expect(playheadSample(transport, 10, 16000, 1)).toBe(500);
    expect(playheadSample(transport, 0.01, 16000, 2)).toBe(420);
  });

  test("media timeline time is not scaled by playback rate a second time", () => {
    const transport = { kind: "linear" as const, startSample: 100, endSample: 1000 };
    // At 2x, 10ms of wall time advances the media timeline by 20ms.
    expect(playheadMediaSample(transport, 0.02, 16000)).toBe(420);
    // At 0.5x, 10ms of wall time advances the media timeline by 5ms.
    expect(playheadMediaSample(transport, 0.005, 16000)).toBe(180);
  });

  test("cut preview jumps over the selected range", () => {
    const pcm = new Int16Array(1000);
    const preview = buildCutPreviewPcm(pcm, 400, 600, 16000);
    expect(preview).not.toBeNull();
    const transport = preview!.transport;
    expect(playheadSample(transport, 0, 16000, 1)).toBe(transport.contextBeforeStart);
    const afterSkip = playheadSample(transport, transport.beforeLen / 16000, 16000, 1);
    expect(afterSkip).toBe(600);
  });

  test("media timeline time maps across the cut-preview jump", () => {
    const preview = buildCutPreviewPcm(new Int16Array(1000), 400, 600, 16000)!;
    expect(playheadMediaSample(preview.transport, preview.transport.beforeLen / 16000, 16000)).toBe(600);
  });
});
