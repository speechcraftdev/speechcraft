export const SETUP_STAGES = Object.freeze([
  'inspect_system',
  'bootstrap_runtime',
  'install_dependencies',
  'install_models',
  'validate_runtime',
]);

function abortError() {
  const error = new Error('operation cancelled');
  error.name = 'AbortError';
  return error;
}

function wait(ms, signal) {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const timer = setTimeout(done, ms);
    function done() {
      signal.removeEventListener('abort', cancel);
      resolve();
    }
    function cancel() {
      clearTimeout(timer);
      signal.removeEventListener('abort', cancel);
      reject(abortError());
    }
    signal.addEventListener('abort', cancel, { once: true });
  });
}

export class SimulatedTasks {
  constructor({ stepDelayMs = 250, stageGate = null, failureInjector = null } = {}) {
    this.stepDelayMs = stepDelayMs;
    this.stageGate = stageGate;
    this.failureInjector = failureInjector;
  }

  async run(stage, { signal, onProgress, attempt }) {
    if (this.failureInjector && await this.failureInjector(stage, attempt)) {
      const error = new Error(`Simulated failure at ${stage}`);
      error.code = 'SIMULATED_STAGE_FAILURE';
      throw error;
    }
    for (const current of [0, 1, 2]) {
      if (signal.aborted) throw abortError();
      onProgress({ current, total: 2, percent: current * 50,
        message: current === 2 ? `${stage} simulation complete` : `${stage} simulation in progress` });
      if (current < 2) await wait(this.stepDelayMs, signal);
    }
    if (this.stageGate) {
      await Promise.race([
        Promise.resolve().then(() => this.stageGate(stage, signal)),
        new Promise((_, reject) => signal.addEventListener('abort', () => reject(abortError()), { once: true })),
      ]);
    }
    if (signal.aborted) throw abortError();
  }
}
