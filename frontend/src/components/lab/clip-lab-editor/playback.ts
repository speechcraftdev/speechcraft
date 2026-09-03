export type LinearTransport = {
  kind: "linear";
  startSample: number;
  endSample: number;
};

export type CutPreviewTransport = {
  kind: "cut-preview";
  contextBeforeStart: number;
  beforeLen: number;
  selectionEnd: number;
  totalSamples: number;
};

export type Transport = LinearTransport | CutPreviewTransport;

const CUT_PREVIEW_CONTEXT_SECONDS = 1;

export function pcm16ToAudioBuffer(
  ctx: AudioContext,
  pcm: Int16Array,
  sampleRateHz: number,
): AudioBuffer {
  const buffer = ctx.createBuffer(1, Math.max(1, pcm.length), sampleRateHz);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) {
    const s = pcm[i];
    channel[i] = s < 0 ? s / 32768 : s / 32767;
  }
  return buffer;
}

export function cutPreviewContextSamples(sampleRateHz: number): number {
  return Math.round(CUT_PREVIEW_CONTEXT_SECONDS * sampleRateHz);
}

export function buildCutPreviewPcm(
  pcm: Int16Array,
  selectionStart: number,
  selectionEnd: number,
  sampleRateHz: number,
): { pcm: Int16Array; transport: CutPreviewTransport } | null {
  if (selectionEnd <= selectionStart) return null;
  const context = cutPreviewContextSamples(sampleRateHz);
  const beforeStart = Math.max(0, selectionStart - context);
  const afterEnd = Math.min(pcm.length, selectionEnd + context);
  const beforeLen = selectionStart - beforeStart;
  const afterLen = afterEnd - selectionEnd;
  if (beforeLen + afterLen <= 0) return null;
  const out = new Int16Array(beforeLen + afterLen);
  out.set(pcm.subarray(beforeStart, selectionStart), 0);
  out.set(pcm.subarray(selectionEnd, afterEnd), beforeLen);
  return {
    pcm: out,
    transport: {
      kind: "cut-preview",
      contextBeforeStart: beforeStart,
      beforeLen,
      selectionEnd,
      totalSamples: out.length,
    },
  };
}

export function playheadSample(
  transport: Transport,
  elapsedSeconds: number,
  fileSampleRateHz: number,
  playbackRate: number,
): number | null {
  const elapsed = Math.max(0, elapsedSeconds * fileSampleRateHz * playbackRate);
  if (transport.kind === "linear") {
    const sample = transport.startSample + elapsed;
    if (sample >= transport.endSample) return transport.endSample;
    return sample;
  }
  if (elapsed >= transport.totalSamples) {
    return transport.selectionEnd + (transport.totalSamples - transport.beforeLen);
  }
  if (elapsed < transport.beforeLen) {
    return transport.contextBeforeStart + elapsed;
  }
  return transport.selectionEnd + (elapsed - transport.beforeLen);
}

export class ClipLabPlayback {
  private ctx: AudioContext | null = null;
  private source: AudioBufferSourceNode | null = null;
  private startedAt = 0;
  private transport: Transport | null = null;
  private fileSampleRateHz = 16000;
  private playbackRate = 1;
  private raf = 0;
  private onStop: (() => void) | null = null;

  setPlaybackRate(rate: number): void {
    this.playbackRate = rate > 0 ? rate : 1;
    if (this.source) this.source.playbackRate.value = this.playbackRate;
  }

  isPlaying(): boolean {
    return this.source !== null;
  }

  currentPlayheadSample(): number | null {
    if (!this.ctx || !this.transport) return null;
    return playheadSample(
      this.transport,
      this.ctx.currentTime - this.startedAt,
      this.fileSampleRateHz,
      this.playbackRate,
    );
  }

  stop(): void {
    if (this.raf) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
    if (this.source) {
      try {
        this.source.onended = null;
        this.source.stop();
      } catch {
        // already stopped
      }
      this.source.disconnect();
      this.source = null;
    }
    this.transport = null;
    const cb = this.onStop;
    this.onStop = null;
    cb?.();
  }

  async playPcm(
    pcm: Int16Array,
    fileSampleRateHz: number,
    transport: Transport,
    onFrame: (sample: number | null) => void,
    onEnded: () => void,
  ): Promise<void> {
    this.stop();
    if (pcm.length === 0) {
      onEnded();
      return;
    }
    this.fileSampleRateHz = fileSampleRateHz;
    this.transport = transport;
    this.onStop = onEnded;
    const ctx = this.ctx ?? new AudioContext();
    this.ctx = ctx;
    if (ctx.state === "suspended") await ctx.resume();
    const source = ctx.createBufferSource();
    source.buffer = pcm16ToAudioBuffer(ctx, pcm, fileSampleRateHz);
    source.playbackRate.value = this.playbackRate;
    source.connect(ctx.destination);
    source.onended = () => {
      if (this.source !== source) return;
      this.source = null;
      this.transport = null;
      if (this.raf) {
        cancelAnimationFrame(this.raf);
        this.raf = 0;
      }
      onFrame(null);
      const cb = this.onStop;
      this.onStop = null;
      cb?.();
    };
    this.source = source;
    this.startedAt = ctx.currentTime;
    let offsetSec = 0;
    let durationSec = pcm.length / fileSampleRateHz;
    if (transport.kind === "linear") {
      offsetSec = transport.startSample / fileSampleRateHz;
      durationSec = Math.max(0, transport.endSample - transport.startSample) / fileSampleRateHz;
    }
    source.start(0, offsetSec, durationSec);
    const tick = () => {
      if (!this.source) return;
      onFrame(this.currentPlayheadSample());
      this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
  }
}
