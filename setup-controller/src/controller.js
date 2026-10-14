import { randomUUID } from 'node:crypto';
import { SETUP_STAGES, SimulatedTasks } from './simulated-tasks.js';

export const JOB_STATES = Object.freeze([
  'idle', 'running', 'waiting_for_user', 'failed', 'cancelled', 'interrupted', 'completed',
]);

const TERMINAL = new Set(['failed', 'cancelled', 'interrupted', 'completed']);
const ALLOWED = Object.freeze({
  idle: new Set(['running']),
  running: new Set(['waiting_for_user', 'cancelling', 'failed', 'interrupted', 'completed']),
  waiting_for_user: new Set(['running', 'cancelling', 'failed', 'interrupted']),
  cancelling: new Set(['cancelled', 'interrupted']),
  failed: new Set(['running']),
  cancelled: new Set(['running']),
  interrupted: new Set(['running']),
  completed: new Set(['running']),
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

function publicJob(job) {
  if (!job) return null;
  const value = structuredClone(job);
  delete value.child_process;
  return value;
}

export function publicStatus(store, controllerStatus = 'running', mode = 'simulated') {
  const state = store.snapshot();
  const job = state.job ? publicJob(state.job) : {
    schema_version: 2,
    id: null, state: 'idle', stage: null, task_id: null, progress: null,
    error: null, attempt_count: 0, revision: state.revision,
    attempt_id: null,
    created_at: null, updated_at: null, started_at: null, completed_at: null,
  };
  return {
    controller_status: controllerStatus,
    job,
    state_revision: state.revision,
    mode,
    simulated: mode === 'simulated',
    setup_plan: state.job?.plan_id || (mode === 'simulated' ? 'simulated-v1' : mode === 'runtime' ? 'speechcraft-runtime-v1' : 'bootstrap-v1'),
    components: state.job?.bootstrap_status?.components || null,
    runtime_profile: state.job?.bootstrap_status?.runtime_profile || null,
    bootstrap_ready: state.job?.bootstrap_status?.bootstrap_ready === true,
    partial_ready: state.job?.bootstrap_status?.partial_ready === true,
    production_ready: false,
  };
}

export class SetupController {
  constructor(store, { tasks = new SimulatedTasks(), mode = 'simulated' } = {}) {
    this.store = store;
    this.tasks = tasks;
    this.mode = mode;
    this.stages = tasks.stages || SETUP_STAGES;
    this.activeExecution = null;
    this.runner = null;
    this.stopping = false;
    const current = this.store.snapshot().job;
    if (current?.state === 'running' || current?.state === 'cancelling') {
      assertTransition(current.state, 'interrupted');
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

  status() { return publicStatus(this.store, this.stopping ? 'stopping' : 'running', this.mode); }

  taskId(stage) { return this.tasks.taskId ? this.tasks.taskId(stage) : `simulated:${stage}`; }

  initialProgress(stage, message) {
    return this.tasks.initialProgress ? this.tasks.initialProgress(stage, message)
      : { current: 0, total: 2, percent: 0, message };
  }

  start() {
    if (this.stopping) throw new ControllerError(503, 'CONTROLLER_STOPPING', 'The setup controller is stopping.');
    const current = this.store.snapshot().job;
    if (current) {
      if (current.state === 'running' || current.state === 'waiting_for_user' || current.state === 'cancelling') {
        return { job: current, idempotent: true };
      }
      if (current.child_process) {
        throw new ControllerError(409, 'PROCESS_RECONCILIATION_REQUIRED',
          'A bootstrap subprocess may still own installation files. Restart the controller to reconcile it before starting setup.');
      }
      if (current.state !== 'completed') {
        throw new ControllerError(409, 'JOB_EXISTS', 'A setup job already exists. Use retry when its state permits.');
      }
    }
    const now = new Date().toISOString();
    const stage = this.stages[0];
    const job = {
      schema_version: 2,
      attempt_id: randomUUID(),
      id: randomUUID(), plan_id: this.mode === 'simulated' ? 'simulated-v1' : this.mode === 'runtime' ? 'speechcraft-runtime-v1' : 'bootstrap-v1', state: 'running', stage,
      task_id: this.taskId(stage), mode: this.mode,
      created_at: now, updated_at: now, started_at: now, completed_at: null,
      attempt_count: 1, revision: 0,
      progress: this.initialProgress(stage, `Queued ${this.mode} setup.`),
      bootstrap_status: this.tasks.initialBootstrapStatus ? this.tasks.initialBootstrapStatus() : null,
      error: null,
    };
    assertTransition(current?.state || 'idle', job.state);
    this.store.commit((state) => { state.job = job; }, 'job_started');
    this.launch();
    return { job: this.store.snapshot().job, idempotent: false };
  }

  retry() {
    if (this.stopping) throw new ControllerError(503, 'CONTROLLER_STOPPING', 'The setup controller is stopping.');
    if (this.activeExecution) {
      throw new ControllerError(409, 'ATTEMPT_STILL_STOPPING', 'The previous setup attempt has not finished stopping yet.');
    }
    const current = this.store.snapshot().job;
    if (!current || !['failed', 'cancelled', 'interrupted'].includes(current.state)) {
      throw new ControllerError(409, 'RETRY_NOT_ALLOWED', 'Retry is allowed only for failed, cancelled, or interrupted jobs.');
    }
    if (current.child_process) {
      throw new ControllerError(409, 'PROCESS_RECONCILIATION_REQUIRED',
        'A bootstrap subprocess may still own installation files. Restart the controller to reconcile it before retrying.');
    }
    const recordedMode = current.mode || 'simulated';
    if (recordedMode !== this.mode) {
      throw new ControllerError(409, 'EXECUTOR_MODE_MISMATCH',
        `This job belongs to ${recordedMode} setup and cannot be retried in ${this.mode} mode.`);
    }
    assertTransition(current.state, 'running');
    const stage = this.stages[0];
    this.store.commit((state) => {
      const job = state.job;
      job.state = 'running';
      job.stage = stage;
      job.task_id = this.taskId(stage);
      job.started_at = new Date().toISOString();
      job.completed_at = null;
      job.attempt_count += 1;
      job.attempt_id = randomUUID();
      job.plan_id ||= recordedMode === 'simulated' ? 'simulated-v1' : recordedMode === 'runtime' ? 'speechcraft-runtime-v1' : 'bootstrap-v1';
      job.error = null;
      job.progress = this.initialProgress(stage, 'Queued setup retry.');
      job.bootstrap_status = this.tasks.initialBootstrapStatus ? this.tasks.initialBootstrapStatus() : job.bootstrap_status;
    }, 'job_retried');
    this.launch();
    return { job: this.store.snapshot().job, idempotent: false };
  }

  cancel(reason = 'USER_CANCELLED') {
    const current = this.store.snapshot().job;
    if (!current) throw new ControllerError(409, 'NO_ACTIVE_JOB', 'There is no setup job to cancel.');
    if (current.state === 'cancelled' || current.state === 'cancelling') return { job: current, idempotent: true };
    if (current.state !== 'running' && current.state !== 'waiting_for_user') {
      throw new ControllerError(409, 'CANCEL_NOT_ALLOWED', `Cannot cancel a ${current.state} setup job.`);
    }
    assertTransition(current.state, 'cancelling');
    this.store.commit((state) => {
      state.job.state = 'cancelling';
      state.job.completed_at = null;
      state.job.error = reason === 'CONTROLLER_SHUTDOWN' ? {
        code: reason, message: 'Setup was cancelled because the controller is shutting down.',
      } : null;
    }, 'job_cancelling');
    const execution = this.activeExecution;
    if (execution && execution.jobId === current.id && execution.attemptId === current.attempt_id) {
      execution.abort.abort();
    } else {
      this.finishCancelled(current.id, current.attempt_id);
    }
    return { job: this.store.snapshot().job, idempotent: false };
  }

  launch() {
    const job = this.store.snapshot().job;
    const abort = new AbortController();
    const execution = { jobId: job.id, attemptId: job.attempt_id, abort, promise: null };
    this.activeExecution = execution;
    execution.promise = this.run(job.id, job.attempt_id, abort.signal)
      .catch(() => {})
      .finally(() => {
        if (this.activeExecution === execution) this.activeExecution = null;
      });
    this.runner = execution.promise;
  }

  isCurrentAttempt(jobId, attemptId) {
    const latest = this.store.snapshot().job;
    return latest?.id === jobId && latest.attempt_id === attemptId;
  }

  finishCancelled(jobId, attemptId) {
    const latest = this.store.snapshot().job;
    if (latest?.id !== jobId || latest.attempt_id !== attemptId || latest.state !== 'cancelling') return;
    const uncertain = Boolean(latest.child_process);
    const targetState = uncertain ? 'interrupted' : 'cancelled';
    assertTransition('cancelling', targetState);
    this.store.commit((state) => {
      state.job.state = targetState;
      state.job.completed_at = new Date().toISOString();
      if (uncertain) {
        state.job.termination_status = 'termination_uncertain';
        state.job.error = { code: 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN',
          message: 'Cancellation could not confirm that all owned bootstrap writers stopped. Retry is blocked until controller recovery confirms termination.' };
      } else {
        state.job.termination_status = 'termination_confirmed';
      }
    }, uncertain ? 'job_termination_uncertain' : 'job_cancelled');
  }

  async run(jobId, attemptId, signal) {
    try {
      for (const stage of this.stages) {
        if (signal.aborted) { this.finishCancelled(jobId, attemptId); return; }
        const current = this.store.snapshot().job;
        if (!current || current.id !== jobId || current.attempt_id !== attemptId || current.state !== 'running') {
          if (current?.state === 'cancelling') this.finishCancelled(jobId, attemptId);
          return;
        }
        this.store.commit((state) => {
          if (state.job?.id !== jobId || state.job.attempt_id !== attemptId || state.job.state !== 'running') return;
          state.job.stage = stage;
          state.job.task_id = this.taskId(stage);
          state.job.progress = this.initialProgress(stage, `${stage} started.`);
        }, 'stage_started');
        await this.tasks.run(stage, {
          signal,
          attempt: current.attempt_count,
          attemptId,
          bootstrapStatus: current.bootstrap_status,
          onComponentStatus: (status) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId || !['running', 'completed'].includes(latest.state)) return;
            this.store.commit((state) => { state.job.bootstrap_status = status; }, 'component_status');
          },
          onChildProcess: (record) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId) return;
            this.store.commit((state) => {
              state.job.child_process = record;
              if (!record) state.job.termination_status = 'termination_confirmed';
              else if (record.termination_status === 'termination_uncertain') state.job.termination_status = 'termination_uncertain';
            }, record ? 'child_process_started' : 'child_process_stopped');
          },
          onProgress: (progress) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId || latest.state !== 'running') return;
            const execution = this.activeExecution;
            if (progress?.phase === 'downloading' && Number.isSafeInteger(progress.current)
                && Number.isSafeInteger(progress.total) && execution?.attemptId === attemptId) {
              const now = Date.now();
              const previous = execution.lastTransferProgress;
              const fileChanged = previous && previous.message !== progress.message;
              if (previous && !fileChanged && progress.current >= previous.current
                  && progress.current - previous.current < 1024 * 1024 && now - previous.at < 750
                  && progress.current !== progress.total) return;
              execution.lastTransferProgress = { current: progress.current, total: progress.total,
                message: progress.message, at: now };
            }
            this.store.commit((state) => { state.job.progress = progress; }, 'task_progress');
          },
        });
        const afterStage = this.store.snapshot().job;
        if (afterStage?.id !== jobId || afterStage.attempt_id !== attemptId) return;
        if (afterStage.state === 'cancelling') { this.finishCancelled(jobId, attemptId); return; }
        if (afterStage.state !== 'running') return;
      }
      const latest = this.store.snapshot().job;
      if (latest?.id === jobId && latest.attempt_id === attemptId && latest.state === 'running') {
        assertTransition('running', 'completed');
        this.store.commit((state) => {
          state.job.state = 'completed';
          state.job.completed_at = new Date().toISOString();
          state.job.progress = this.tasks.completionProgress
            ? this.tasks.completionProgress()
            : { current: 2, total: 2, percent: 100, message: 'Simulated setup completed.' };
        }, 'job_completed');
      }
    } catch (error) {
      const current = this.store.snapshot().job;
      if (current?.id !== jobId || current.attempt_id !== attemptId) return;
      if (current.state === 'cancelling') { this.finishCancelled(jobId, attemptId); return; }
      if (current.state !== 'running') return;
      const interrupted = Boolean(current.child_process);
      this.store.commit((state) => {
        state.job.state = interrupted ? 'interrupted' : 'failed';
        state.job.completed_at = new Date().toISOString();
        if (interrupted) state.job.termination_status = 'termination_uncertain';
        state.job.error = {
          code: /^[A-Z0-9_]+$/.test(error?.code || '') ? error.code : 'SETUP_TASK_FAILED',
          message: /^[A-Z0-9_]+$/.test(error?.code || '') && error.code === 'SIMULATED_STAGE_FAILURE'
            ? `A simulated setup stage failed (${state.job.stage}).`
            : ['BootstrapError', 'PlatformError'].includes(error?.name) && typeof error.message === 'string'
              ? error.message.slice(0, 320)
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
