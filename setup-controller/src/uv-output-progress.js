const PACKAGE_LINE = /^\+ ([A-Za-z0-9][A-Za-z0-9._-]{0,127})==([A-Za-z0-9][A-Za-z0-9._+!~-]{0,95})$/;
const SUMMARY_LINE = /^(Resolved|Prepared|Installed) (\d+) packages? in /;
const MAX_PARTIAL = 4096;

/** Parse only package install records observed from uv 0.12.23 text output. Never forwards raw subprocess text. */
export class UvOutputProgress {
  constructor(onEvent = () => {}, { clock = () => Date.now(), minIntervalMs = 250 } = {}) {
    this.onEvent = onEvent;
    this.clock = clock;
    this.minIntervalMs = minIntervalMs;
    this.partials = new Map();
    this.seen = new Set();
    this.lastEventAt = 0;
    this.pending = null;
    this.timer = null;
  }

  push(_stream, chunk) {
    try {
      const stream = _stream === 'stdout' ? 'stdout' : 'stderr';
      const partial = `${this.partials.get(stream) || ''}${Buffer.from(chunk).toString('utf8')}`.slice(-MAX_PARTIAL);
      const lines = partial.split(/\r\n|\n|\r/);
      this.partials.set(stream, lines.pop() || '');
      for (const line of lines) this.parseLine(line);
    } catch { /* telemetry cannot affect the child process or its result */ }
  }

  flush() {
    for (const [stream, partial] of this.partials) {
      if (!partial) continue;
      this.partials.set(stream, '');
      const line = partial;
      this.parseLine(line);
    }
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (this.pending) this.emit(this.pending);
    this.pending = null;
  }

  queue(event) {
    const now = this.clock();
    if (!this.lastEventAt || now - this.lastEventAt >= this.minIntervalMs) {
      this.emit(event);
      return;
    }
    this.pending = event;
    if (!this.timer) this.timer = setTimeout(() => {
      this.timer = null;
      const pending = this.pending;
      this.pending = null;
      if (pending) this.emit(pending);
    }, this.minIntervalMs - (now - this.lastEventAt));
    this.timer.unref?.();
  }

  emit(event) {
    this.lastEventAt = this.clock();
    try { this.onEvent(event); } catch { /* telemetry cannot affect installation */ }
  }

  parseLine(line) {
    try {
      const packageMatch = PACKAGE_LINE.exec(line.trim());
      if (packageMatch) {
        const identity = `${packageMatch[1]}==${packageMatch[2]}`;
        if (this.seen.has(identity) || this.seen.size >= 4096) return;
        this.seen.add(identity);
        this.queue({ kind: 'package_installed', operation: 'installing', artifact_name: identity,
          message: `Installed ${identity}.` });
        return;
      }
      const summary = SUMMARY_LINE.exec(line.trim());
      if (summary) this.queue({ kind: 'package_summary', operation: summary[1] === 'Resolved' ? 'resolving' : 'installing', count: Number(summary[2]),
        message: `${summary[1]} ${summary[2]} Python packages.` });
    } catch { /* ignore parser and callback failures */ }
  }
}
