import { describe, expect, test } from "bun:test";
import { drawWaveform, resizeCanvasToCss, type WaveformTheme } from "./waveform-draw";

const theme: WaveformTheme = {
  wave: "#454545",
  selectionFill: "rgba(0,0,0,0.1)",
  selectionEdge: "#737373",
  cursor: "#f5f5f5",
  playhead: "#d4d4d4",
  progress: "rgba(0,0,0,0.1)",
};

function fakeCanvas(initialWidth = 0, initialHeight = 0) {
  const calls: string[] = [];
  const ctx = {
    setTransform: (...args: number[]) => {
      calls.push(`setTransform:${args.join(",")}`);
    },
    clearRect: (x: number, y: number, w: number, h: number) => {
      calls.push(`clearRect:${x},${y},${w},${h}`);
    },
    beginPath: () => {
      calls.push("beginPath");
    },
  };
  const canvas = {
    width: initialWidth,
    height: initialHeight,
    style: { width: "", height: "" },
    getContext: () => ctx,
  };
  return { canvas: canvas as unknown as HTMLCanvasElement, calls };
}

describe("resizeCanvasToCss", () => {
  test("clears the full device-pixel backing store with identity transform", () => {
    const { canvas, calls } = fakeCanvas();
    resizeCanvasToCss(canvas, 800, 200, 2);
    expect(canvas.width).toBe(1600);
    expect(canvas.height).toBe(400);
    expect(calls).toEqual([
      "setTransform:1,0,0,1,0,0",
      "clearRect:0,0,1600,400",
      "beginPath",
    ]);
  });

  test("still clears leftover pixels when backing store size is unchanged", () => {
    const { canvas, calls } = fakeCanvas(1600, 400);
    resizeCanvasToCss(canvas, 800, 200, 2);
    expect(calls).toContain("clearRect:0,0,1600,400");
  });
});

describe("drawWaveform min-max", () => {
  test("fills one min/max bar per CSS column spanning the matching device pixels", () => {
    const rects: Array<{ x: number; w: number }> = [];
    let strokeCalls = 0;
    const ctx = {
      canvas: { width: 40, height: 20 },
      setTransform() {},
      clearRect() {},
      beginPath() {},
      stroke() {
        strokeCalls += 1;
      },
      fill() {},
      fillRect(x: number, _y: number, w: number) {
        rects.push({ x, w });
      },
      imageSmoothingEnabled: true,
      strokeStyle: "",
      fillStyle: "",
      lineWidth: 1,
    } as unknown as CanvasRenderingContext2D;
    const pcm = new Int16Array(200);
    for (let i = 0; i < pcm.length; i++) pcm[i] = i % 2 === 0 ? 1000 : -1000;
    drawWaveform(
      ctx,
      pcm,
      { viewStartSample: 0, viewEndSample: 200, widthCssPx: 20 },
      20,
      10,
      1000,
      theme,
      2,
    );
    expect(strokeCalls).toBe(0);
    expect(rects).toHaveLength(20);
    expect(rects.every((r) => r.w === 2)).toBe(true);
    expect(rects.map((r) => r.x)).toEqual(Array.from({ length: 20 }, (_, i) => i * 2));
  });
});
