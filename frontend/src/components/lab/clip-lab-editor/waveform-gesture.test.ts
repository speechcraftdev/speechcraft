import { describe, expect, test } from "bun:test";
import { clickWaveform, createTimelineState, mappingFromState, xFromSample } from "./timeline";
import { cursorDragAnchorSample, CURSOR_HIT_PX } from "./waveform-editor";

describe("cursor drag acquisition", () => {
  test("within the CSS-pixel hit radius anchors to the exact cursor sample", () => {
    const state = createTimelineState(48000, 48000);
    const mapping = mappingFromState(state, 480);
    const cursorX = xFromSample(12000, mapping);
    expect(cursorDragAnchorSample(12000, cursorX + CURSOR_HIT_PX, mapping)).toBe(12000);
  });

  test("outside the hit radius leaves the pointer-down sample as anchor", () => {
    const state = createTimelineState(48000, 48000);
    const mapping = mappingFromState(state, 480);
    const cursorX = xFromSample(12000, mapping);
    expect(cursorDragAnchorSample(12000, cursorX + CURSOR_HIT_PX + 0.01, mapping)).toBeNull();
  });

  test("a click still moves the cursor to the actual clicked sample", () => {
    const state = clickWaveform(createTimelineState(48000, 48000), 12020);
    expect(state.cursorSample).toBe(12020);
    expect(state.selection).toBeNull();
  });

  test("the hit radius remains CSS-pixel based at different zoom levels", () => {
    const state = createTimelineState(48000, 48000);
    const wide = mappingFromState(state, 480);
    const zoomed = { ...state, viewStartSample: 10000, viewEndSample: 20000 };
    const closeToCursor = xFromSample(12000, wide) + CURSOR_HIT_PX;
    const closeToZoomedCursor = xFromSample(12000, mappingFromState(zoomed, 480)) + CURSOR_HIT_PX;
    expect(cursorDragAnchorSample(12000, closeToCursor, wide)).toBe(12000);
    expect(cursorDragAnchorSample(12000, closeToZoomedCursor, mappingFromState(zoomed, 480))).toBe(12000);
  });

  test("both drag directions acquire the same exact anchor", () => {
    const state = createTimelineState(48000, 48000);
    const mapping = mappingFromState(state, 480);
    const cursorX = xFromSample(24000, mapping);
    expect(cursorDragAnchorSample(24000, cursorX - 3, mapping)).toBe(24000);
    expect(cursorDragAnchorSample(24000, cursorX + 3, mapping)).toBe(24000);
  });

  test("cursor at the clip boundaries is acquired without leaving the clip", () => {
    const state = createTimelineState(48000, 48000);
    const mapping = mappingFromState(state, 480);
    expect(cursorDragAnchorSample(0, 0, mapping)).toBe(0);
    expect(cursorDragAnchorSample(state.durationSamples, 480, mapping)).toBe(state.durationSamples);
  });
});
