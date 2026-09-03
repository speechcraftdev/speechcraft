import { describe, expect, test } from "bun:test";
import {
  assertTimelineInvariants,
  clampView,
  clickWaveform,
  createTimelineState,
  dragSelect,
  extendSelection,
  fitView,
  hasSelection,
  insertSilence,
  insertSilenceSampleCount,
  mappingFromState,
  moveCursor,
  panView,
  preserveViewAfterDurationChange,
  resizeGrabbedEndpoint,
  rippleDelete,
  sampleFromX,
  samplesPerPixel,
  selectEntireClip,
  selectionEnd,
  selectionFromAnchorFocus,
  selectionStart,
  shiftClick,
  splicePcmDelete,
  splicePcmInsertSilence,
  xFromSample,
  zoomAround,
  zoomToSelection,
  type TimelineState,
} from "./timeline";

const WIDTH = 200;

function mapping(state: TimelineState, width = WIDTH) {
  return mappingFromState(state, width);
}

function expectInvariants(state: TimelineState) {
  expect(() => assertTimelineInvariants(state)).not.toThrow();
}

describe("coordinate mapping", () => {
  test("fit view maps sample 0 to left edge and duration to right edge", () => {
    const state = createTimelineState(800, 16000);
    const m = mapping(state);
    expect(xFromSample(0, m)).toBe(0);
    expect(xFromSample(800, m)).toBe(WIDTH);
    expect(sampleFromX(0, m)).toBe(0);
    expect(sampleFromX(WIDTH, m)).toBe(800);
  });

  test("sample → x → sample round trip stays within mapping precision", () => {
    const state = createTimelineState(1000, 16000);
    const m = mapping(state, 80);
    const spp = samplesPerPixel(m);
    for (const sample of [0, 1, 17, 250, 999, 1000]) {
      const recovered = sampleFromX(xFromSample(sample, m), m);
      expect(Math.abs(recovered - sample)).toBeLessThanOrEqual(Math.ceil(spp / 2) + 1);
    }
  });

  test("zoomed viewport does not treat widget-right as clip EOF", () => {
    const state = {
      ...createTimelineState(1000, 16000),
      viewStartSample: 100,
      viewEndSample: 300,
    };
    const m = mapping(state);
    expect(sampleFromX(WIDTH, m)).toBe(300);
    expect(sampleFromX(WIDTH, m)).not.toBe(1000);
    expect(xFromSample(1000, m)).toBeGreaterThan(WIDTH);
    expect(xFromSample(300, m)).toBe(WIDTH);
    expect(xFromSample(100, m)).toBe(0);
  });
});

describe("selection", () => {
  test("click collapses selection to cursor", () => {
    let state = dragSelect(createTimelineState(100, 16000), 10, 40);
    expect(hasSelection(state)).toBe(true);
    state = clickWaveform(state, 25);
    expect(state.selection).toBeNull();
    expect(state.cursorSample).toBe(25);
  });

  test("drag left-to-right and right-to-left preserve direction", () => {
    const ltr = dragSelect(createTimelineState(100, 16000), 10, 40);
    expect(ltr.selection).toEqual({ anchorSample: 10, focusSample: 40 });
    expect(selectionStart(ltr.selection!)).toBe(10);
    expect(selectionEnd(ltr.selection!)).toBe(40);

    const rtl = dragSelect(createTimelineState(100, 16000), 40, 10);
    expect(rtl.selection).toEqual({ anchorSample: 40, focusSample: 10 });
    expect(selectionStart(rtl.selection!)).toBe(10);
    expect(selectionEnd(rtl.selection!)).toBe(40);
  });

  test("shift-click from cursor uses cursor as anchor", () => {
    const state = shiftClick(clickWaveform(createTimelineState(100, 16000), 20), 35);
    expect(state.selection).toEqual({ anchorSample: 20, focusSample: 35 });
  });

  test("shift extension preserves active direction", () => {
    const selected = dragSelect(createTimelineState(100, 16000), 10, 40);
    const extended = shiftClick(selected, 55);
    expect(extended.selection).toEqual({ anchorSample: 10, focusSample: 55 });
    const reversed = dragSelect(createTimelineState(100, 16000), 40, 10);
    expect(shiftClick(reversed, 2).selection).toEqual({ anchorSample: 40, focusSample: 2 });
  });

  test("resize each edge and crossing stays continuous", () => {
    const selected = dragSelect(createTimelineState(100, 16000), 10, 40);
    const moveStart = resizeGrabbedEndpoint(selected, "anchor", 25);
    expect(moveStart.selection).toEqual({ anchorSample: 25, focusSample: 40 });
    const crossed = resizeGrabbedEndpoint(selected, "anchor", 55);
    expect(selectionStart(crossed.selection!)).toBe(40);
    expect(selectionEnd(crossed.selection!)).toBe(55);
    expect(crossed.selection?.anchorSample).toBe(55);
    expect(crossed.selection?.focusSample).toBe(40);
    const moveEnd = resizeGrabbedEndpoint(selected, "focus", 12);
    expect(selectionStart(moveEnd.selection!)).toBe(10);
    expect(selectionEnd(moveEnd.selection!)).toBe(12);
  });

  test("one-sample selection is valid and start===end is not used for empty", () => {
    const state = dragSelect(createTimelineState(100, 16000), 7, 7);
    expect(state.selection).not.toBeNull();
    expect(selectionEnd(state.selection!) - selectionStart(state.selection!)).toBe(1);
    expect(state.selection).not.toEqual({ anchorSample: 7, focusSample: 7 });
    expectInvariants(state);
  });
});

describe("keyboard", () => {
  test("Left/Right move exactly 1 sample and clamp", () => {
    let state = clickWaveform(createTimelineState(10, 16000), 5);
    state = moveCursor(state, 1);
    expect(state.cursorSample).toBe(6);
    state = moveCursor(state, -1);
    expect(state.cursorSample).toBe(5);
    state = clickWaveform(state, 0);
    expect(moveCursor(state, -1).cursorSample).toBe(0);
    state = clickWaveform(state, 10);
    expect(moveCursor(state, 1).cursorSample).toBe(10);
  });

  test("selection collapses to the Audacity edge before a later move", () => {
    const selected = dragSelect(createTimelineState(100, 16000), 10, 40);
    expect(moveCursor(selected, -1)).toMatchObject({ cursorSample: 10, selection: null });
    expect(moveCursor(selected, 1)).toMatchObject({ cursorSample: 40, selection: null });
  });

  test("Shift+Left/Right adjusts focus by exactly 1 sample", () => {
    let state = dragSelect(createTimelineState(100, 16000), 10, 40);
    state = extendSelection(state, 1);
    expect(state.selection).toEqual({ anchorSample: 10, focusSample: 41 });
    state = extendSelection(state, -1);
    expect(state.selection).toEqual({ anchorSample: 10, focusSample: 40 });
    const fromCursor = extendSelection(clickWaveform(createTimelineState(100, 16000), 5), 1);
    expect(fromCursor.selection).toEqual({ anchorSample: 5, focusSample: 6 });
  });
});

describe("view", () => {
  test("fit shows the entire clip", () => {
    const zoomed = { ...createTimelineState(500, 16000), viewStartSample: 40, viewEndSample: 80 };
    expect(fitView(zoomed)).toMatchObject({ viewStartSample: 0, viewEndSample: 500 });
  });

  test("zoom around a point keeps that sample stationary within rounding", () => {
    const state = createTimelineState(1000, 16000);
    const width = 200;
    const x = 50;
    const before = sampleFromX(x, mapping(state, width));
    const zoomed = zoomAround(state, before, x, width, 2);
    const afterX = xFromSample(before, mapping(zoomed, width));
    expect(Math.abs(afterX - x)).toBeLessThanOrEqual(2);
    expectInvariants(zoomed);
  });

  test("pan clamps to clip bounds", () => {
    const zoomed = { ...createTimelineState(1000, 16000), viewStartSample: 100, viewEndSample: 300 };
    expect(panView(zoomed, -10000).viewStartSample).toBe(0);
    expect(panView(zoomed, 10000).viewEndSample).toBe(1000);
    const panned = panView(zoomed, 50);
    expect(panned.viewEndSample - panned.viewStartSample).toBe(200);
    expectInvariants(panned);
  });

  test("zoom-to-selection uses the selected range", () => {
    const state = dragSelect(createTimelineState(1000, 16000), 100, 180);
    const zoomed = zoomToSelection(state);
    expect(zoomed.viewStartSample).toBe(100);
    expect(zoomed.viewEndSample).toBe(180);
  });

  test("viewport remains valid after clip duration changes", () => {
    const state = { ...createTimelineState(1000, 16000), viewStartSample: 800, viewEndSample: 1000 };
    const view = preserveViewAfterDurationChange(state, 500);
    expect(view.viewEndSample).toBeLessThanOrEqual(500);
    expect(view.viewStartSample).toBeGreaterThanOrEqual(0);
    expect(view.viewEndSample).toBeGreaterThan(view.viewStartSample);
    const clamped = clampView(0, 0, 10);
    expect(clamped.viewEndSample).toBeGreaterThan(clamped.viewStartSample);
  });
});

describe("delete", () => {
  test("middle, prefix, suffix, one-sample, and whole-clip refusal", () => {
    const n = 20;
    const middle = rippleDelete(dragSelect(createTimelineState(n, 16000), 5, 10));
    expect(middle.ok).toBe(true);
    if (middle.ok) {
      expect(middle.state.durationSamples).toBe(n - 5);
      expect(middle.state.cursorSample).toBe(5);
      expect(middle.state.selection).toBeNull();
    }

    const prefix = rippleDelete(dragSelect(createTimelineState(n, 16000), 0, 4));
    expect(prefix.ok && prefix.state.durationSamples).toBe(n - 4);
    expect(prefix.ok && prefix.state.cursorSample).toBe(0);

    const suffix = rippleDelete(dragSelect(createTimelineState(n, 16000), 15, 20));
    expect(suffix.ok && suffix.state.durationSamples).toBe(n - 5);
    expect(suffix.ok && suffix.state.cursorSample).toBe(15);

    const one = rippleDelete(dragSelect(createTimelineState(n, 16000), 3, 4));
    expect(one.ok && one.state.durationSamples).toBe(n - 1);

    const whole = rippleDelete(selectEntireClip(createTimelineState(n, 16000)));
    expect(whole).toEqual({ ok: false, reason: "entire_clip" });
    expect(rippleDelete(createTimelineState(n, 16000))).toEqual({ ok: false, reason: "no_selection" });
  });
});

describe("insert", () => {
  test("inserts exact zeros at cursor and selects the new range", () => {
    const pcm = new Int16Array([1, 2, 3, 4, 5]);
    const state = clickWaveform(createTimelineState(pcm.length, 16000), 2);
    const inserted = insertSilence(state, 3);
    expect(inserted.atSample).toBe(2);
    expect(inserted.durationSamples).toBe(3);
    expect(inserted.state.durationSamples).toBe(8);
    expect(inserted.state.selection).toEqual({ anchorSample: 2, focusSample: 5 });
    const next = splicePcmInsertSilence(pcm, inserted.atSample, inserted.durationSamples);
    expect(Array.from(next)).toEqual([1, 2, 0, 0, 0, 3, 4, 5]);
  });

  test("inserts at the selection left edge without replacing audio", () => {
    const selected = dragSelect(createTimelineState(10, 48000), 6, 9);
    const inserted = insertSilence(selected, insertSilenceSampleCount(48000));
    expect(inserted.atSample).toBe(6);
    expect(inserted.durationSamples).toBe(Math.round(0.2 * 48000));
    expect(inserted.state.durationSamples).toBe(10 + inserted.durationSamples);
  });

  test("splicePcmDelete removes the half-open range", () => {
    expect(Array.from(splicePcmDelete(new Int16Array([0, 1, 2, 3, 4]), 1, 3))).toEqual([0, 3, 4]);
  });
});

describe("randomized invariants", () => {
  test("valid operation sequences never produce illegal state", () => {
    let rng = 1;
    const rand = () => {
      rng = (rng * 1664525 + 1013904223) >>> 0;
      return rng / 0x100000000;
    };
    const randInt = (lo: number, hi: number) => lo + Math.floor(rand() * (hi - lo + 1));

    for (let trial = 0; trial < 40; trial++) {
      let duration = randInt(8, 400);
      let state = createTimelineState(duration, 16000);
      let pcm = new Int16Array(duration);
      for (let i = 0; i < duration; i++) pcm[i] = i;
      for (let step = 0; step < 80; step++) {
        const kind = randInt(0, 5);
        if (kind === 0) {
          state = clickWaveform(state, randInt(0, state.durationSamples));
        } else if (kind === 1) {
          state = dragSelect(state, randInt(0, state.durationSamples), randInt(0, state.durationSamples));
        } else if (kind === 2) {
          const m = mapping(state, 120);
          const x = rand() * 120;
          state = zoomAround(state, sampleFromX(x, m), x, 120, rand() < 0.5 ? 1.4 : 1 / 1.4);
        } else if (kind === 3) {
          state = panView(state, randInt(-80, 80));
        } else if (kind === 4 && state.selection) {
          const result = rippleDelete(state);
          if (result.ok) {
            pcm = splicePcmDelete(pcm, result.startSample, result.endSample);
            state = result.state;
          }
        } else {
          const inserted = insertSilence(state, randInt(1, 12));
          pcm = splicePcmInsertSilence(pcm, inserted.atSample, inserted.durationSamples);
          state = inserted.state;
        }
        if (state.durationSamples <= 0) {
          duration = randInt(8, 40);
          state = createTimelineState(duration, 16000);
          pcm = new Int16Array(duration);
        }
        expect(pcm.length).toBe(state.durationSamples);
        expectInvariants(state);
      }
    }
  });
});

describe("selectionFromAnchorFocus", () => {
  test("does not represent empty selection as start === end", () => {
    expect(selectionFromAnchorFocus(4, 4, 10)).toEqual({ anchorSample: 4, focusSample: 5 });
  });
});
