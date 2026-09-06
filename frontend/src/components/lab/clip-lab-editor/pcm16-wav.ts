export class ClipLabWavError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ClipLabWavError";
  }
}

export type ParsedClipLabWav = {
  sampleRateHz: number;
  pcm: Int16Array;
};

const RIFF = 0x46464952;
const WAVE = 0x45564157;
const FMT = 0x20746d66;
const DATA = 0x61746164;
const PCM_FORMAT = 1;
const MONO = 1;
const BITS_PER_SAMPLE = 16;
const BLOCK_ALIGN = 2;

function u16(view: DataView, offset: number): number {
  return view.getUint16(offset, true);
}

function u32(view: DataView, offset: number): number {
  return view.getUint32(offset, true);
}

function requireBytes(length: number, offset: number, size: number, label: string): void {
  if (offset < 0 || size < 0 || offset + size > length) {
    throw new ClipLabWavError(`${label}: chunk extends beyond buffer`);
  }
}

/**
 * Strict PCM16 mono WAV parser for Clip Lab.
 * Supports RIFF/WAVE format-1 mono s16le, skips unknown chunks, honors even-byte padding.
 */
export function parseClipLabPcm16MonoWav(buffer: ArrayBuffer): ParsedClipLabWav {
  if (buffer.byteLength < 12) {
    throw new ClipLabWavError("WAV is truncated before RIFF header");
  }
  const view = new DataView(buffer);
  if (u32(view, 0) !== RIFF) {
    throw new ClipLabWavError("expected RIFF header");
  }
  const riffSize = u32(view, 4);
  if (riffSize < 4 || 8 + riffSize > buffer.byteLength) {
    throw new ClipLabWavError("RIFF size is truncated or invalid");
  }
  if (u32(view, 8) !== WAVE) {
    throw new ClipLabWavError("expected WAVE form type");
  }

  let offset = 12;
  const riffEnd = 8 + riffSize;
  let sampleRateHz: number | null = null;
  let dataOffset: number | null = null;
  let dataSize: number | null = null;
  let sawFmt = false;

  while (offset + 8 <= riffEnd) {
    const chunkId = u32(view, offset);
    const chunkSize = u32(view, offset + 4);
    const dataStart = offset + 8;
    requireBytes(buffer.byteLength, dataStart, chunkSize, "chunk payload");
    if (dataStart + chunkSize > riffEnd) {
      throw new ClipLabWavError("chunk payload exceeds RIFF bounds");
    }

    if (chunkId === FMT) {
      if (chunkSize < 16) {
        throw new ClipLabWavError("fmt chunk is truncated");
      }
      const audioFormat = u16(view, dataStart);
      const channels = u16(view, dataStart + 2);
      const sampleRate = u32(view, dataStart + 4);
      const byteRate = u32(view, dataStart + 8);
      const blockAlign = u16(view, dataStart + 12);
      const bitsPerSample = u16(view, dataStart + 14);
      if (audioFormat !== PCM_FORMAT) {
        throw new ClipLabWavError(`unsupported audioFormat ${audioFormat}; expected PCM 1`);
      }
      if (channels !== MONO) {
        throw new ClipLabWavError(`expected mono WAV, got ${channels} channels`);
      }
      if (sampleRate <= 0) {
        throw new ClipLabWavError(`expected positive sample rate, got ${sampleRate}`);
      }
      if (bitsPerSample !== BITS_PER_SAMPLE) {
        throw new ClipLabWavError(`expected 16-bit PCM, got ${bitsPerSample} bits`);
      }
      if (blockAlign !== BLOCK_ALIGN) {
        throw new ClipLabWavError(`expected blockAlign ${BLOCK_ALIGN}, got ${blockAlign}`);
      }
      const expectedByteRate = sampleRate * BLOCK_ALIGN;
      if (byteRate !== expectedByteRate) {
        throw new ClipLabWavError(`expected byteRate ${expectedByteRate}, got ${byteRate}`);
      }
      sampleRateHz = sampleRate;
      sawFmt = true;
    } else if (chunkId === DATA) {
      if (chunkSize % 2 !== 0) {
        throw new ClipLabWavError("data chunk length is not divisible by 2-byte PCM frames");
      }
      dataOffset = dataStart;
      dataSize = chunkSize;
    }

    const padded = chunkSize + (chunkSize % 2);
    offset = dataStart + padded;
  }

  if (!sawFmt || sampleRateHz === null) {
    throw new ClipLabWavError("missing fmt chunk");
  }
  if (dataOffset === null || dataSize === null) {
    throw new ClipLabWavError("missing data chunk");
  }

  const sampleCount = dataSize / 2;
  if (!Number.isInteger(sampleCount) || sampleCount < 0) {
    throw new ClipLabWavError("PCM sample count is invalid");
  }
  const pcm = new Int16Array(sampleCount);
  for (let i = 0; i < sampleCount; i++) {
    pcm[i] = view.getInt16(dataOffset + i * 2, true);
  }
  return { sampleRateHz, pcm };
}

export function assertClipLabWavMatchesManifest(
  parsed: ParsedClipLabWav,
  expected: { sampleRateHz: number; durationSamples: number },
): void {
  if (parsed.sampleRateHz !== expected.sampleRateHz) {
    throw new ClipLabWavError(
      `sample rate mismatch: wav ${parsed.sampleRateHz} Hz vs manifest ${expected.sampleRateHz} Hz`,
    );
  }
  if (parsed.pcm.length !== expected.durationSamples) {
    throw new ClipLabWavError(
      `sample count mismatch: wav ${parsed.pcm.length} vs duration_samples ${expected.durationSamples}`,
    );
  }
}
