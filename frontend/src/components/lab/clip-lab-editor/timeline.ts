export type Selection = {
  anchorSample: number;
  focusSample: number;
};

export type TimelineState = {
  durationSamples: number;
  sampleRateHz: number;
  cursorSample: number;
  selection: Selection | null;
  viewStartSample: number;
  viewEndSample: number;
};

export type ViewMapping = {
  viewStartSample: number;
  viewEndSample: number;
  widthCssPx: number;
};

export type DeleteResult =
  | { ok: true; state: TimelineState; startSample: number; endSample: number }
  | { ok: false; reason: "no_selection" | "entire_clip" };

export const INSERT_SILENCE_SECONDS = 0.2;
export const MIN_VIEW_SAMPLES = 2;
export const ZERO_CROSS_WINDOW_SAMPLES = 1024;

export function clampInt(value: number, lo: number, hi: number): number {
  if (value < lo) return lo;
  if (value > hi) return hi;
  return value;
}

export function selectionStart(selection: Selection): number {
  return Math.min(selection.anchorSample, selection.focusSample);
}

export function selectionEnd(selection: Selection): number {
  return Math.max(selection.anchorSample, selection.focusSample);
}

export function hasSelection(state: TimelineState): boolean {
  return state.selection !== null;
}

export function insertSilenceSampleCount(sampleRateHz: number): number {
  return Math.round(INSERT_SILENCE_SECONDS * sampleRateHz);
}

export function createTimelineState(durationSamples: number, sampleRateHz: number): TimelineState {
  const duration = Math.max(0, Math.floor(durationSamples));
  return {
    durationSamples: duration,
    sampleRateHz,
    cursorSample: 0,
    selection: null,
    viewStartSample: 0,
    viewEndSample: duration,
  };
}

export function assertTimelineInvariants(state: TimelineState): void {
  const { durationSamples, cursorSample, selection, viewStartSample, viewEndSample } = state;
  if (cursorSample < 0 || cursorSample > durationSamples) {
    throw new Error(`cursor ${cursorSample} outside [0, ${durationSamples}]`);
  }
  if (selection) {
    const start = selectionStart(selection);
    const end = selectionEnd(selection);
    if (!(start >= 0 && start < end && end <= durationSamples)) {
      throw new Error(`invalid selection [${start}, ${end}) for duration ${durationSamples}`);
    }
  }
  if (!(viewStartSample >= 0 && viewStartSample < viewEndSample && viewEndSample <= durationSamples)) {
    throw new Error(
      `invalid view [${viewStartSample}, ${viewEndSample}] for duration ${durationSamples}`,
    );
  }
}

export function samplesPerPixel(mapping: ViewMapping): number {
  if (mapping.widthCssPx <= 0) {
    return mapping.viewEndSample - mapping.viewStartSample;
  }
  return (mapping.viewEndSample - mapping.viewStartSample) / mapping.widthCssPx;
}

/** Authoritative hit-test mapping. Left edge is viewStart; right edge is viewEnd. */
export function sampleFromX(xCssPx: number, mapping: ViewMapping): number {
  const span = mapping.viewEndSample - mapping.viewStartSample;
  if (mapping.widthCssPx <= 0 || span <= 0) return mapping.viewStartSample;
  const t = xCssPx / mapping.widthCssPx;
  return Math.round(mapping.viewStartSample + t * span);
}

/** Authoritative draw mapping. Inverse of sampleFromX at the view edges. */
export function xFromSample(sample: number, mapping: ViewMapping): number {
  const span = mapping.viewEndSample - mapping.viewStartSample;
  if (span <= 0 || mapping.widthCssPx <= 0) return 0;
  return ((sample - mapping.viewStartSample) / span) * mapping.widthCssPx;
}

export function sampleRangeForPixelColumn(xCssPx: number, mapping: ViewMapping): { start: number; end: number } {
  const spp = samplesPerPixel(mapping);
  const start = mapping.viewStartSample + xCssPx * spp;
  const end = mapping.viewStartSample + (xCssPx + 1) * spp;
  return { start, end };
}

export function mappingFromState(state: TimelineState, widthCssPx: number): ViewMapping {
  return {
    viewStartSample: state.viewStartSample,
    viewEndSample: state.viewEndSample,
    widthCssPx,
  };
}

export function clampSampleToClip(sample: number, durationSamples: number): number {
  return clampInt(Math.round(sample), 0, durationSamples);
}

export function selectionFromAnchorFocus(
  anchorSample: number,
  focusSample: number,
  durationSamples: number,
): Selection | null {
  let anchor = clampSampleToClip(anchorSample, durationSamples);
  let focus = clampSampleToClip(focusSample, durationSamples);
  if (anchor === focus) {
    if (focus < durationSamples) focus = anchor + 1;
    else if (anchor > 0) anchor = focus - 1;
    else return null;
  }
  return { anchorSample: anchor, focusSample: focus };
}

export function clickWaveform(state: TimelineState, sample: number): TimelineState {
  return {
    ...state,
    selection: null,
    cursorSample: clampSampleToClip(sample, state.durationSamples),
  };
}

export function dragSelect(state: TimelineState, anchorSample: number, focusSample: number): TimelineState {
  return {
    ...state,
    selection: selectionFromAnchorFocus(anchorSample, focusSample, state.durationSamples),
    cursorSample: clampSampleToClip(anchorSample, state.durationSamples),
  };
}

/**
 * Shift-click: cursor becomes the anchor when there is no selection.
 * If a selection exists, move focus and keep the existing anchor/direction.
 */
export function shiftClick(state: TimelineState, sample: number): TimelineState {
  const clicked = clampSampleToClip(sample, state.durationSamples);
  if (!state.selection) {
    return {
      ...state,
      selection: selectionFromAnchorFocus(state.cursorSample, clicked, state.durationSamples),
    };
  }
  return {
    ...state,
    selection: selectionFromAnchorFocus(state.selection.anchorSample, clicked, state.durationSamples),
  };
}

export function selectEntireClip(state: TimelineState): TimelineState {
  if (state.durationSamples <= 0) return { ...state, selection: null, cursorSample: 0 };
  if (state.durationSamples === 1) {
    return { ...state, selection: { anchorSample: 0, focusSample: 1 }, cursorSample: 0 };
  }
  return {
    ...state,
    selection: { anchorSample: 0, focusSample: state.durationSamples },
    cursorSample: 0,
  };
}

/**
 * Resize the endpoint that was the start (min) at pointer-down.
 * Crossing the other edge keeps the same field moving so the range stays valid.
 */
export function resizeGrabbedEndpoint(
  state: TimelineState,
  grabbed: "anchor" | "focus",
  sample: number,
): TimelineState {
  if (!state.selection) return state;
  const next = clampSampleToClip(sample, state.durationSamples);
  if (grabbed === "anchor") {
    return {
      ...state,
      selection: selectionFromAnchorFocus(next, state.selection.focusSample, state.durationSamples),
    };
  }
  return {
    ...state,
    selection: selectionFromAnchorFocus(state.selection.anchorSample, next, state.durationSamples),
  };
}

export function endpointAtX(
  selection: Selection,
  mapping: ViewMapping,
  xCssPx: number,
  hitPx: number,
): "anchor" | "focus" | null {
  const startX = xFromSample(selectionStart(selection), mapping);
  const endX = xFromSample(selectionEnd(selection), mapping);
  const distStart = Math.abs(xCssPx - startX);
  const distEnd = Math.abs(xCssPx - endX);
  const startHit = distStart <= hitPx;
  const endHit = distEnd <= hitPx;
  if (!startHit && !endHit) return null;
  const startIsAnchor = selection.anchorSample <= selection.focusSample;
  if (startHit && endHit) {
    const useStart = distStart <= distEnd;
    return useStart === startIsAnchor ? "anchor" : "focus";
  }
  if (startHit) return startIsAnchor ? "anchor" : "focus";
  return startIsAnchor ? "focus" : "anchor";
}

/** Audacity-style: collapse selection to that edge; otherwise move exactly 1 sample. */
export function moveCursor(state: TimelineState, deltaSamples: number): TimelineState {
  if (state.selection) {
    const edge = deltaSamples < 0 ? selectionStart(state.selection) : selectionEnd(state.selection);
    return {
      ...state,
      selection: null,
      cursorSample: clampSampleToClip(edge, state.durationSamples),
    };
  }
  return {
    ...state,
    cursorSample: clampSampleToClip(state.cursorSample + deltaSamples, state.durationSamples),
  };
}

export function extendSelection(state: TimelineState, deltaSamples: number): TimelineState {
  if (!state.selection) {
    return {
      ...state,
      selection: selectionFromAnchorFocus(
        state.cursorSample,
        state.cursorSample + deltaSamples,
        state.durationSamples,
      ),
    };
  }
  return {
    ...state,
    selection: selectionFromAnchorFocus(
      state.selection.anchorSample,
      state.selection.focusSample + deltaSamples,
      state.durationSamples,
    ),
  };
}

function minViewLength(durationSamples: number): number {
  if (durationSamples <= 1) return Math.max(durationSamples, 0);
  return Math.min(durationSamples, MIN_VIEW_SAMPLES);
}

export function clampView(
  viewStartSample: number,
  viewEndSample: number,
  durationSamples: number,
): { viewStartSample: number; viewEndSample: number } {
  if (durationSamples <= 0) {
    return { viewStartSample: 0, viewEndSample: 0 };
  }
  const minLen = minViewLength(durationSamples);
  let start = Math.round(viewStartSample);
  let end = Math.round(viewEndSample);
  let len = end - start;
  if (len < minLen) len = minLen;
  if (len > durationSamples) len = durationSamples;
  start = clampInt(start, 0, Math.max(0, durationSamples - len));
  end = start + len;
  if (end > durationSamples) {
    end = durationSamples;
    start = Math.max(0, end - len);
  }
  if (end <= start) {
    return { viewStartSample: 0, viewEndSample: durationSamples };
  }
  return { viewStartSample: start, viewEndSample: end };
}

export function fitView(state: TimelineState): TimelineState {
  return {
    ...state,
    viewStartSample: 0,
    viewEndSample: state.durationSamples,
  };
}

export function panView(state: TimelineState, deltaSamples: number): TimelineState {
  const len = state.viewEndSample - state.viewStartSample;
  const next = clampView(state.viewStartSample + deltaSamples, state.viewStartSample + deltaSamples + len, state.durationSamples);
  return { ...state, ...next };
}

export function zoomAround(
  state: TimelineState,
  sampleUnderPointer: number,
  xCssPx: number,
  widthCssPx: number,
  factor: number,
): TimelineState {
  if (state.durationSamples <= 0 || widthCssPx <= 0 || factor <= 0) return state;
  const oldLen = state.viewEndSample - state.viewStartSample;
  const minLen = minViewLength(state.durationSamples);
  let newLen = oldLen / factor;
  newLen = clampInt(Math.round(newLen), minLen, state.durationSamples);
  const ratio = xCssPx / widthCssPx;
  const anchor = sampleUnderPointer;
  let start = anchor - ratio * newLen;
  let end = start + newLen;
  const clamped = clampView(start, end, state.durationSamples);
  return { ...state, ...clamped };
}

export function zoomToSelection(state: TimelineState): TimelineState {
  if (!state.selection) return state;
  const start = selectionStart(state.selection);
  const end = selectionEnd(state.selection);
  return { ...state, ...clampView(start, end, state.durationSamples) };
}

export function preserveViewAfterDurationChange(
  state: TimelineState,
  newDurationSamples: number,
): { viewStartSample: number; viewEndSample: number } {
  const oldLen = state.viewEndSample - state.viewStartSample;
  return clampView(state.viewStartSample, state.viewStartSample + oldLen, newDurationSamples);
}

export function rippleDelete(state: TimelineState): DeleteResult {
  if (!state.selection) return { ok: false, reason: "no_selection" };
  const startSample = selectionStart(state.selection);
  const endSample = selectionEnd(state.selection);
  if (startSample === 0 && endSample === state.durationSamples) {
    return { ok: false, reason: "entire_clip" };
  }
  const newDuration = state.durationSamples - (endSample - startSample);
  const view = preserveViewAfterDurationChange(state, newDuration);
  return {
    ok: true,
    startSample,
    endSample,
    state: {
      ...state,
      durationSamples: newDuration,
      selection: null,
      cursorSample: clampSampleToClip(startSample, newDuration),
      viewStartSample: view.viewStartSample,
      viewEndSample: view.viewEndSample,
    },
  };
}

export function insertSilence(
  state: TimelineState,
  durationSamples: number,
): { state: TimelineState; atSample: number; durationSamples: number } {
  const atSample = state.selection
    ? selectionStart(state.selection)
    : clampSampleToClip(state.cursorSample, state.durationSamples);
  const insertCount = Math.max(0, Math.round(durationSamples));
  const newDuration = state.durationSamples + insertCount;
  const view = preserveViewAfterDurationChange(state, newDuration);
  const selection =
    insertCount > 0 ? { anchorSample: atSample, focusSample: atSample + insertCount } : null;
  return {
    atSample,
    durationSamples: insertCount,
    state: {
      ...state,
      durationSamples: newDuration,
      cursorSample: atSample,
      selection,
      viewStartSample: view.viewStartSample,
      viewEndSample: view.viewEndSample,
    },
  };
}

export function splicePcmDelete(pcm: Int16Array, startSample: number, endSample: number): Int16Array {
  const next = new Int16Array(pcm.length - (endSample - startSample));
  next.set(pcm.subarray(0, startSample), 0);
  next.set(pcm.subarray(endSample), startSample);
  return next;
}

export function splicePcmInsertSilence(pcm: Int16Array, atSample: number, durationSamples: number): Int16Array {
  const next = new Int16Array(pcm.length + durationSamples);
  next.set(pcm.subarray(0, atSample), 0);
  next.set(pcm.subarray(atSample), atSample + durationSamples);
  return next;
}

export function nearestZeroCrossing(pcm: Int16Array, sample: number, windowSamples: number): number {
  if (pcm.length <= 0) return 0;
  const origin = clampInt(Math.round(sample), 0, pcm.length);
  const lo = Math.max(0, origin - windowSamples);
  const hi = Math.min(pcm.length, origin + windowSamples);
  let bestIndex = origin;
  let bestDist = Number.POSITIVE_INFINITY;
  const consider = (index: number) => {
    const dist = Math.abs(index - origin);
    if (dist < bestDist || (dist === bestDist && index < bestIndex)) {
      bestDist = dist;
      bestIndex = index;
    }
  };
  for (let i = lo; i < hi; i++) {
    if (pcm[i] === 0) consider(i);
    if (i > lo) {
      const prev = pcm[i - 1];
      const cur = pcm[i];
      if ((prev < 0 && cur > 0) || (prev > 0 && cur < 0)) consider(i);
    }
  }
  return clampInt(bestIndex, 0, pcm.length);
}

export function snapSelectionToZeroCrossings(
  state: TimelineState,
  pcm: Int16Array,
  windowSamples: number = ZERO_CROSS_WINDOW_SAMPLES,
): TimelineState {
  if (!state.selection) return state;
  const start = selectionStart(state.selection);
  const end = selectionEnd(state.selection);
  let snappedStart = nearestZeroCrossing(pcm, start, windowSamples);
  let snappedEnd = nearestZeroCrossing(pcm, end, windowSamples);
  snappedStart = clampInt(snappedStart, 0, state.durationSamples);
  snappedEnd = clampInt(snappedEnd, 0, state.durationSamples);
  const selection = selectionFromAnchorFocus(
    state.selection.anchorSample <= state.selection.focusSample ? snappedStart : snappedEnd,
    state.selection.anchorSample <= state.selection.focusSample ? snappedEnd : snappedStart,
    state.durationSamples,
  );
  return selection ? { ...state, selection } : state;
}
