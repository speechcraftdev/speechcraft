import { describe, expect, test } from "bun:test";
import {
  ClipLabWavError,
  assertClipLabWavMatchesManifest,
  parseClipLabPcm16MonoWav,
} from "./pcm16-wav";

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
}

export function encodePcm16MonoWav(
  samples: ArrayLike<number>,
  sampleRateHz: number,
  extraChunks: { id: string; payload: Uint8Array }[] = [],
): ArrayBuffer {
  const dataSize = samples.length * 2;
  let extraSize = 0;
  for (const chunk of extraChunks) {
    extraSize += 8 + chunk.payload.length + (chunk.payload.length % 2);
  }
  const fmtSize = 16;
  const riffSize = 4 + (8 + fmtSize) + extraSize + (8 + dataSize);
  const buffer = new ArrayBuffer(8 + riffSize);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, riffSize, true);
  writeAscii(view, 8, "WAVE");
  let offset = 12;
  writeAscii(view, offset, "fmt ");
  view.setUint32(offset + 4, fmtSize, true);
  view.setUint16(offset + 8, 1, true);
  view.setUint16(offset + 10, 1, true);
  view.setUint32(offset + 12, sampleRateHz, true);
  view.setUint32(offset + 16, sampleRateHz * 2, true);
  view.setUint16(offset + 20, 2, true);
  view.setUint16(offset + 22, 16, true);
  offset += 8 + fmtSize;
  for (const chunk of extraChunks) {
    writeAscii(view, offset, chunk.id);
    view.setUint32(offset + 4, chunk.payload.length, true);
    new Uint8Array(buffer, offset + 8, chunk.payload.length).set(chunk.payload);
    offset += 8 + chunk.payload.length + (chunk.payload.length % 2);
  }
  writeAscii(view, offset, "data");
  view.setUint32(offset + 4, dataSize, true);
  const pcm = new Int16Array(buffer, offset + 8, samples.length);
  pcm.set(samples);
  return buffer;
}

describe("parseClipLabPcm16MonoWav", () => {
  test("parses PCM16 mono and copies samples", () => {
    const src = new Int16Array([0, -32768, 32767, 12]);
    const parsed = parseClipLabPcm16MonoWav(encodePcm16MonoWav(src, 16000));
    expect(parsed.sampleRateHz).toBe(16000);
    expect(Array.from(parsed.pcm)).toEqual(Array.from(src));
  });

  test("skips unknown chunks and honors odd-size padding", () => {
    const src = new Int16Array([1, 2, 3]);
    const parsed = parseClipLabPcm16MonoWav(
      encodePcm16MonoWav(src, 48000, [{ id: "LIST", payload: new Uint8Array([1, 2, 3]) }]),
    );
    expect(parsed.sampleRateHz).toBe(48000);
    expect(Array.from(parsed.pcm)).toEqual([1, 2, 3]);
  });

  test("rejects stereo, non-PCM, and truncated files", () => {
    const stereo = encodePcm16MonoWav(new Int16Array([1, 2]), 16000);
    const view = new DataView(stereo);
    view.setUint16(22, 2, true);
    expect(() => parseClipLabPcm16MonoWav(stereo)).toThrow(ClipLabWavError);

    const nonPcm = encodePcm16MonoWav(new Int16Array([1, 2]), 16000);
    new DataView(nonPcm).setUint16(20, 3, true);
    expect(() => parseClipLabPcm16MonoWav(nonPcm)).toThrow(/audioFormat/);

    expect(() => parseClipLabPcm16MonoWav(new ArrayBuffer(8))).toThrow(/truncated/);
  });

  test("assertClipLabWavMatchesManifest rejects resampling mismatches", () => {
    const parsed = parseClipLabPcm16MonoWav(encodePcm16MonoWav(new Int16Array([1, 2, 3, 4]), 16000));
    expect(() =>
      assertClipLabWavMatchesManifest(parsed, { sampleRateHz: 48000, durationSamples: 4 }),
    ).toThrow(/sample rate mismatch/);
    expect(() =>
      assertClipLabWavMatchesManifest(parsed, { sampleRateHz: 16000, durationSamples: 8 }),
    ).toThrow(/sample count mismatch/);
    assertClipLabWavMatchesManifest(parsed, { sampleRateHz: 16000, durationSamples: 4 });
    expect(() =>
      assertClipLabWavMatchesManifest(parsed, { sampleRateHz: 16000, durationSamples: 5 }),
    ).toThrow(/sample count mismatch/);
    expect(() =>
      assertClipLabWavMatchesManifest(parsed, { sampleRateHz: 16000, durationSamples: 3 }),
    ).toThrow(/sample count mismatch/);
  });

  test("rejects contradictory byteRate", () => {
    const buffer = encodePcm16MonoWav(new Int16Array([1, 2, 3, 4]), 16000);
    new DataView(buffer).setUint32(28, 999, true);
    expect(() => parseClipLabPcm16MonoWav(buffer)).toThrow(/byteRate/);
  });

  test("decodes PCM samples as little-endian int16", () => {
    const buffer = encodePcm16MonoWav(new Int16Array(2), 16000);
    const view = new DataView(buffer);
    // data chunk starts after RIFF/fmt: offset 44
    view.setUint8(44, 0x00);
    view.setUint8(45, 0x01);
    view.setUint8(46, 0x00);
    view.setUint8(47, 0x80);
    const parsed = parseClipLabPcm16MonoWav(buffer);
    expect(Array.from(parsed.pcm)).toEqual([256, -32768]);
  });
});
