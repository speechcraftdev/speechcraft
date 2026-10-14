import { randomUUID } from 'node:crypto';
import { SETUP_STAGES, SimulatedTasks } from './simulated-tasks.js';

export const JOB_STATES = Object.freeze([
  'idle', 'running', 'waiting_for_user', 'failed', 'cancelled', 'interrupted', 'completed',
]);

const TERMINAL = new Set(['failed', 'cancelled', 'interrupted', 'completed']);
const ALLOWED = Object.freeze({
  idle: new Set(['running']),
  running: new Set(['waiting_for_user', 'failed', 'cancelled', 'interrupted', 'completed']),
  waiting_for_user: new Set(['running', 'cancelled', 'failed', 'interrupted']),
  failed: new Set(['running']),
  cancelled: new Set(['running']),
  interrupted: new Set(['running']),
  completed: new Set(),
});

export class ControllerError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = 'ControllerError';
    this.status = status;
    this.code = code;
  }
}

export function assertTransition(from, to) {
  if (!ALLOWED[from]?.has(to)) {
    throw new ControllerError(409, 'INVALID_STATE_TRANSITION', `Cannot transition setup from ${from} to ${to}.`);
  }
}

export function publicStatus(store, controllerStatus = 'running') {
  const state = store.snapshot();
  const job = state.job ? structuredClone(state.job) : {
    schema_version: 1,
    id: null, state: 'idle', stage: null, task_id: null, progress: null,
    error: null, attempt_count: 0, revision: state.revision,
    created_at: null, updated_at: null, started_at: null, completed_at: null,
  };
  return {
    controller_status: controllerStatus,
    job,
    state_revision: state.revision,
    simulated: true,
    production_ready: false,
  };
}

export class SetupController {
  constructor(store, { tasks = new SimulatedTasks() } = {}) {
    this.store = store;
    this.tasks = tasks;
    this.abortController = null;
    this.runner = null;
    this.stopping = false;
    const current = this.store.snapshot().job;
    if (current?.state === 'running') {
      assertTransition('running', 'interrupted');
      this.store.commit((state) => {
        state.job.state = 'interrupted';
        state.job.completed_at = new Date().toISOString();
        state.job.error = {
          code: 'CONTROLLER_RESTARTED',
          message: 'The controller stopped before this setup job completed. Retry to continue.',
        };
      }, 'recovered_interrupted');
    }
  }

  status() { return publicStatus(this.store, this.stopping ? 'stopping' : 'running'); }

  start() {
    if (this.stopping) throw new ControllerError(503, 'CONTROLLER_STOPPING', 'The setup controller is stopping.');
    const current = this.store.snapshot().job;
    if (current) {
      if (current.state === 'running' || current.state === 'waiting_for_user') {
        return { job: current, idempotent: true };
      }
      throw new ControllerError(409, 'JOB_EXISTS', 'A setup job already exists. Use retry when its state permits.');
    }
    const now = new Date().toISOString();
    const job = {
      schema_version: 1,
      id: randomUUID(), state: 'running', stage: SETUP_STAGES[0],
      task_id: `simulated:${SETUP_STAGES[0]}`,
      created_at: now, updated_at: now, started_at: now, completed_at: null,
      attempt_count: 1, revision: 0,
      progress: { current: 0, total: 2, percent: 0, message: 'Queued simulated setup.' },
      error: null,
    };
    assertTransition('idle', job.state);
    this.store.commit((state) => { state.job = job; }, 'job_started');
    this.launch();
    return { job: this.store.snapshot().job, idempotent: false };
  }

  retry() {
    if (this.stopping) throw new ControllerError(503, 'CONTROLLER_STOPPING', 'The setup controller is stopping.');
    const current = this.store.snapshot().job;
    if (!current || !['failed', 'cancelled', 'interrupted'].includes(current.state)) {
      throw new ControllerError(409, 'RETRY_NOT_ALLOWED', 'Retry is allowed only for failed, cancelled, or interrupted jobs.');
    }
    assertTransition(current.state, 'running');
    this.store.commit((state) => {
      const job = state.job;
      job.state = 'running';
      job.stage = SETUP_STAGES[0];
      job.task_id = `simulated:${SETUP_STAGES[0]}`;
      job.started_at = new Date().toISOString();
      job.completed_at = null;
      job.attempt_count += 1;
      job.error = null;
      job.progress = { current: 0, total: 2, percent: 0, message: 'Queued setup retry.' };
    }, 'job_retried');
    this.launch();
    return { job: this.store.snapshot().job, idempotent: false };
  }

  cancel(reason = 'USER_CANCELLED') {
    const current = this.store.snapshot().job;
    if (!current) throw new ControllerError(409, 'NO_ACTIVE_JOB', 'There is no setup job to cancel.');
    if (current.state === 'cancelled') return { job: current, idempotent: true };
    if (current.state !== 'running' && current.state !== 'waiting_for_user') {
      throw new ControllerError(409, 'CANCEL_NOT_ALLOWED', `Cannot cancel a ${current.state} setup job.`);
    }
    assertTransition(current.state, 'cancelled');
    this.store.commit((state) => {
      state.job.state = 'cancelled';
      state.job.completed_at = new Date().toISOString();
      state.job.error = reason === 'CONTROLLER_SHUTDOWN' ? {
        code: reason, message: 'Setup was cancelled because the controller is shutting down.',
      } : null;
    }, 'job_cancelled');
    this.abortController?.abort();
    return { job: this.store.snapshot().job, idempotent: false };
  }

  launch() {
    const jobId = this.store.snapshot().job.id;
    const abort = new AbortController();
    this.abortController = abort;
    this.runner = this.run(jobId, abort.signal).catch(() => {});
  }

  async run(jobId, signal) {
    try {
      for (const stage of SETUP_STAGES) {
        if (signal.aborted) return;
        const current = this.store.snapshot().job;
        if (!current || current.id !== jobId || current.state !== 'running') return;
        this.store.commit((state) => {
          state.job.stage = stage;
          state.job.task_id = `simulated:${stage}`;
          state.job.progress = { current: 0, total: 2, percent: 0, message: `${stage} simulation started.` };
        }, 'stage_started');
        await this.tasks.run(stage, {
          signal,
          attempt: current.attempt_count,
          onProgress: (progress) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.state !== 'running') return;
            this.store.commit((state) => { state.job.progress = progress; }, 'task_progress');
          },
        });
      }
      const latest = this.store.snapshot().job;
      if (latest?.id === jobId && latest.state === 'running') {
        assertTransition('running', 'completed');
        this.store.commit((state) => {
          state.job.state = 'completed';
          state.job.completed_at = new Date().toISOString();
          state.job.progress = { current: 2, total: 2, percent: 100, message: 'Simulated setup completed.' };
        }, 'job_completed');
      }
    } catch (error) {
      const current = this.store.snapshot().job;
      if (current?.id !== jobId || current.state !== 'running') return;
      this.store.commit((state) => {
        state.job.state = 'failed';
        state.job.completed_at = new Date().toISOString();
        state.job.error = {
          code: /^[A-Z0-9_]+$/.test(error?.code || '') ? error.code : 'SETUP_TASK_FAILED',
          message: /^[A-Z0-9_]+$/.test(error?.code || '') && error.code === 'SIMULATED_STAGE_FAILURE'
            ? `A simulated setup stage failed (${state.job.stage}).`
            : 'A setup stage failed. Review diagnostics before retrying.',
        };
      }, 'job_failed');
    }
  }

  async waitForIdle() { await this.runner; }

  async shutdown() {
    this.stopping = true;
    const current = this.store.snapshot().job;
    if (current?.state === 'running' || current?.state === 'waiting_for_user') {
      this.cancel('CONTROLLER_SHUTDOWN');
    }
    await this.runner;
  }
}
