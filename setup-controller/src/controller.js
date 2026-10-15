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

const PUBLIC_JOB_FIELDS = Object.freeze(['schema_version', 'id', 'plan_id', 'state', 'stage', 'task_id', 'mode',
  'created_at', 'updated_at', 'started_at', 'completed_at', 'attempt_count', 'attempt_id', 'revision', 'progress', 'error']);
const PUBLIC_COMPONENT_NAMES = new Set(['uv', 'managed_python', 'model_manager', 'backend_environment', 'worker_environment',
  'worker_cuda_candidate', 'ffmpeg', 'silero', 'whisper', 'titanet', 'pyannote', 'runtime_probes', 'backend_service', 'dataset_processing']);
const PUBLIC_COMPONENT_FIELDS = Object.freeze(['identity', 'state', 'verification', 'last_verified_at', 'failure_code',
  'message', 'runtime_ready', 'progress', 'progress_indeterminate', 'device', 'effective_profile', 'release_blockers',
  'action_required', 'components', 'blocked_components', 'optional_blocked_components', 'inference_completed']);

function publicComponentField(key, value) {
  if (key === 'components' || key === 'blocked_components' || key === 'optional_blocked_components') {
    return Array.isArray(value) ? value.filter((item) => typeof item === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(item)).slice(0, 32) : [];
  }
  if (key === 'release_blockers') {
    return Array.isArray(value) ? value.slice(0, 32).map((item) => ({
      component: typeof item?.component === 'string' && PUBLIC_COMPONENT_NAMES.has(item.component) ? item.component : 'unknown',
      code: typeof item?.code === 'string' && /^[A-Z0-9_]{1,80}$/.test(item.code) ? item.code : 'RELEASE_BLOCKED',
      message: typeof item?.message === 'string' ? item.message.slice(0, 320) : 'A release approval is unresolved.',
    })) : [];
  }
  if (key === 'message') return typeof value === 'string' ? value.slice(0, 320) : null;
  if (key === 'failure_code') return typeof value === 'string' && /^[A-Z0-9_]{1,80}$/.test(value) ? value : null;
  return structuredClone(value);
}

export function publicComponents(components) {
  if (!components || typeof components !== 'object') return null;
  return Object.fromEntries(Object.entries(components).filter(([name]) => PUBLIC_COMPONENT_NAMES.has(name)).map(([name, value]) => [name,
    Object.fromEntries(PUBLIC_COMPONENT_FIELDS.filter((key) => value?.[key] !== undefined)
      .map((key) => [key, publicComponentField(key, value[key])]))]));
}

export function serializePublicJob(job) {
  if (!job) return null;
  const value = Object.fromEntries(PUBLIC_JOB_FIELDS.filter((key) => job[key] !== undefined)
    .map((key) => [key, structuredClone(job[key])]));
  if (job.error) value.error = {
    code: typeof job.error.code === 'string' && /^[A-Z0-9_]{1,80}$/.test(job.error.code) ? job.error.code : 'SETUP_TASK_FAILED',
    message: typeof job.error.message === 'string' ? job.error.message.slice(0, 320) : 'A setup operation failed.',
  };
  if (job.progress) value.progress = Object.fromEntries(['phase', 'current', 'total', 'percent', 'message', 'speed_bytes_per_second', 'indeterminate']
    .filter((key) => job.progress[key] !== undefined).map((key) => [key, structuredClone(job.progress[key])]));
  if (job.bootstrap_status) {
    value.bootstrap_status = {
      profile: job.bootstrap_status.profile || null,
      wsl: job.bootstrap_status.wsl === true,
      uv_verified: job.bootstrap_status.uv_verified === true,
      python_verified: job.bootstrap_status.python_verified === true,
      model_manager_verified: job.bootstrap_status.model_manager_verified === true,
      bootstrap_ready: job.bootstrap_status.bootstrap_ready === true,
      partial_ready: job.bootstrap_status.partial_ready === true,
      production_ready: job.bootstrap_status.production_ready === true,
      technical_processing_ready: job.bootstrap_status.technical_processing_ready === true,
      release_blockers: Array.isArray(job.bootstrap_status.release_blockers) ? structuredClone(job.bootstrap_status.release_blockers) : [],
      components: publicComponents(job.bootstrap_status.components),
      runtime_profile: job.bootstrap_status.runtime_profile ? {
        profile: job.bootstrap_status.runtime_profile.profile || null,
        workerProfile: job.bootstrap_status.runtime_profile.workerProfile || null,
        modelPlatform: job.bootstrap_status.runtime_profile.modelPlatform || null,
        accelerator: job.bootstrap_status.runtime_profile.accelerator || null,
        cuda_candidate: job.bootstrap_status.runtime_profile.cuda_candidate === true,
        cuda_validated: job.bootstrap_status.runtime_profile.cuda_validated === true,
      } : null,
    };
  }
  return value;
}

export function publicStatus(store, controllerStatus = 'running', mode = 'simulated') {
  const state = store.snapshot();
  const job = state.job ? serializePublicJob(state.job) : {
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
    components: publicComponents(state.job?.bootstrap_status?.components),
    runtime_profile: serializePublicJob(state.job)?.bootstrap_status?.runtime_profile || null,
    technical_processing_ready: state.job?.bootstrap_status?.technical_processing_ready === true,
    bootstrap_ready: state.job?.bootstrap_status?.bootstrap_ready === true,
    partial_ready: state.job?.bootstrap_status?.partial_ready === true,
    production_ready: state.job?.bootstrap_status?.production_ready === true,
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
      if (current.child_process || current.termination_status === 'termination_uncertain') {
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
    if (current.child_process || current.termination_status === 'termination_uncertain') {
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
      // Keep prior component evidence until each trusted stage revalidates it.
      // In particular, an interrupted gated acquisition must be able to tell
      // the user that its ephemeral credential was discarded.
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

  authorizeModel(role, credential) {
    if (role !== 'diarization') throw new ControllerError(404, 'MODEL_ROLE_NOT_FOUND', 'This setup model role is not available for authorization.');
    if (this.mode !== 'runtime') throw new ControllerError(409, 'RUNTIME_SETUP_REQUIRED', 'Community-1 authorization is available only during runtime setup.');
    if (typeof credential !== 'string' || !/^hf_[A-Za-z0-9_-]{16,512}$/.test(credential)) {
      throw new ControllerError(400, 'HF_TOKEN_INVALID', 'Enter a valid Hugging Face read token.');
    }
    if (this.activeExecution) throw new ControllerError(409, 'AUTHORIZATION_ALREADY_RUNNING', 'A setup operation is still stopping or running. Wait for its status before submitting a token.');
    const current = this.store.snapshot().job;
    if (!current || current.mode !== 'runtime' || current.stage !== 'install_models' || current.state !== 'waiting_for_user') {
      if (current?.bootstrap_status?.components?.pyannote?.state === 'runtime_ready') {
        return { job: current, idempotent: true };
      }
      throw new ControllerError(409, 'AUTHORIZATION_NOT_REQUESTED', 'Start or resume setup and wait until Community-1 requests authorization.');
    }
    assertTransition('waiting_for_user', 'running');
    const attemptId = randomUUID();
    this.store.commit((state) => {
      const job = state.job;
      job.state = 'running';
      job.attempt_id = attemptId;
      job.attempt_count += 1;
      job.completed_at = null;
      job.error = null;
      job.progress = this.initialProgress('install_models', 'Checking Community-1 access.');
      const component = job.bootstrap_status?.components?.pyannote;
      if (component) Object.assign(component, { state: 'checking_token', failure_code: null, message: null });
    }, 'model_authorization_started');
    this.launch({ startStage: 'install_models', authorizationToken: Buffer.from(credential, 'utf8') });
    return { job: this.store.snapshot().job, idempotent: false };
  }

  launch({ startStage = null, authorizationToken = null } = {}) {
    const job = this.store.snapshot().job;
    const abort = new AbortController();
    const execution = { jobId: job.id, attemptId: job.attempt_id, abort, promise: null,
      startStage, authorizationToken };
    this.activeExecution = execution;
    execution.promise = this.run(job.id, job.attempt_id, abort.signal, execution)
      .catch(() => {})
      .finally(() => {
        if (Buffer.isBuffer(execution.authorizationToken)) execution.authorizationToken.fill(0);
        execution.authorizationToken = null;
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

  async run(jobId, attemptId, signal, execution = null) {
    try {
      const firstStage = execution?.startStage ? this.stages.indexOf(execution.startStage) : 0;
      if (firstStage < 0) throw new ControllerError(500, 'SETUP_STAGE_INVALID', 'The requested setup stage is unavailable.');
      for (const stage of this.stages.slice(firstStage)) {
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
        try { await this.tasks.run(stage, {
          signal,
          attempt: current.attempt_count,
          attemptId,
          bootstrapStatus: current.bootstrap_status,
          authorizationToken: stage === 'install_models' ? execution?.authorizationToken || null : null,
          onComponentStatus: (status) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId || !['running', 'completed'].includes(latest.state)) return;
            this.store.commit((state) => { state.job.bootstrap_status = status; }, 'component_status');
          },
          onChildProcess: (record, phase) => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId) return false;
            if (phase === 'credential_release') {
              const owned = latest.child_process;
              return Boolean(record && owned && owned.pid === record.pid && owned.nonce === record.nonce
                && owned.identity === record.identity && owned.attempt_id === attemptId
                && latest.state === 'running' && latest.stage === 'install_models');
            }
            this.store.commit((state) => {
              state.job.child_process = record;
              if (!record) state.job.termination_status = 'termination_confirmed';
              else if (record.termination_status === 'termination_uncertain') state.job.termination_status = 'termination_uncertain';
            }, record ? 'child_process_started' : 'child_process_stopped');
            return true;
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
          waitForUser: (componentStatus, message = 'Waiting for model authorization.') => {
            const latest = this.store.snapshot().job;
            if (latest?.id !== jobId || latest.attempt_id !== attemptId || latest.state !== 'running') return;
            this.store.commit((state) => {
              state.job.state = 'waiting_for_user';
              state.job.completed_at = null;
              state.job.progress = { current: null, total: null, percent: null, message };
              if (componentStatus && state.job.bootstrap_status) state.job.bootstrap_status = componentStatus;
            }, 'waiting_for_user');
          },
        }); } finally {
          if (stage === 'install_models' && Buffer.isBuffer(execution?.authorizationToken)) {
            execution.authorizationToken.fill(0);
            execution.authorizationToken = null;
          }
        }
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
      const authCode = ['HF_TOKEN_INVALID', 'HF_MODEL_ACCESS_REQUIRED', 'HF_TOKEN_PERMISSION_DENIED',
        'HF_APPROVAL_PENDING', 'AUTHORIZATION_REQUIRED_AFTER_RESTART'].includes(error?.code);
      if (authCode && current.stage === 'install_models' && !current.child_process) {
        const messages = {
          HF_TOKEN_INVALID: 'Hugging Face rejected the token. Check it and submit a valid read token.',
          HF_MODEL_ACCESS_REQUIRED: 'Accept the Community-1 conditions on Hugging Face and ensure this account has access, then submit a read token.',
          HF_TOKEN_PERMISSION_DENIED: 'This token lacks read permission. Create a read token and retry.',
          HF_APPROVAL_PENDING: 'Hugging Face has not granted Community-1 access yet. Complete its approval steps and retry.',
          AUTHORIZATION_REQUIRED_AFTER_RESTART: 'The controller restarted before gated acquisition completed. Submit the read token again to continue.',
        };
        this.store.commit((state) => {
          state.job.state = 'waiting_for_user';
          state.job.completed_at = null;
          state.job.error = { code: error.code, message: messages[error.code] };
          state.job.progress = { current: null, total: null, percent: null, message: 'Waiting for valid Community-1 authorization.' };
        }, 'model_authorization_required');
        return;
      }
      const ownershipUncertain = ['BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'BOOTSTRAP_OWNERSHIP_PERSIST_FAILED'].includes(error?.code);
      const interrupted = Boolean(current.child_process) || ownershipUncertain;
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
    // Waiting for user is durable idle state, not active work. Preserve it
    // across a graceful restart so the next controller can continue asking
    // for the ephemeral authorization without creating a cancelled job.
    if (current?.state === 'running') {
      this.cancel('CONTROLLER_SHUTDOWN');
    }
    await this.runner;
  }
}
