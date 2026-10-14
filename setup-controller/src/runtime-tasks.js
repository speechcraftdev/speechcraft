import { BootstrapTasks } from './bootstrap.js';
import { PRODUCTION_STAGES } from './runtime-plan.js';
import { BackendService, reconcileOwnedService } from './backend-service.js';
import path from 'node:path';

/** Full setup orchestration. Unsupported recipes are represented as blockers, not successful installs. */
export class RuntimeTasks extends BootstrapTasks {
  constructor(options = {}) {
    super(options);
    this.stages = PRODUCTION_STAGES;
    this.service = null;
    this.store = options.store;
  }

  updateServiceStatus(status, record) {
    const current = this.store?.snapshot().job;
    if (!current?.bootstrap_status) return;
    const owned = this.store.snapshot().service_process;
    if (record && owned && (owned.pid !== record.pid || owned.nonce !== record.nonce)) return;
    if (record && !owned && status.state !== 'stopped') return;
    this.store.commit((state) => {
      const components = state.job.bootstrap_status.components || {};
      components.backend_service = { ...(components.backend_service || {}), ...status };
      state.job.bootstrap_status.components = components;
      state.job.bootstrap_status.production_ready = false;
    }, 'backend_service_status');
  }

  async startServices(context) {
    const backend = context.componentStatus.components?.backend_environment;
    if (backend?.state !== 'verified') {
      this.setComponent(context, 'backend_service', { state: 'blocked', failure_code: 'backend_environment_unavailable', message: 'The verified backend environment is unavailable; service startup was skipped.' });
      return;
    }
    if (this.service?.record && this.service.child?.exitCode === null
        && this.service.child?.signalCode === null) {
      try {
        const result = await this.service.verify();
        this.setComponent(context, 'backend_service', { state: 'verified', verification: 'loopback_healthz', last_verified_at: new Date().toISOString(), failure_code: null, message: null, port: result.port });
        return;
      } catch { await this.service.stop(); }
    } else if (this.store?.snapshot().service_process) {
      await reconcileOwnedService(this.store);
    }
    const target = this.backendEnvironmentPath();
    this.service = new BackendService({ store: this.store, dataDirectory: this.dataDirectory,
      applicationRoot: this.applicationRoot, environment: this.environment,
      pythonPath: path.join(target, 'bin', 'python'), environmentVars: this.makeEnvironment(this.paths),
      attemptId: context.attemptId,
      workerRoot: path.join(this.applicationRoot, 'workers', 'dataset'),
      workerPython: path.join(this.dataDirectory, 'environments', 'worker', 'unavailable', 'bin', 'python'),
      onStatus: (status, record) => this.updateServiceStatus(status, record),
    });
    await this.service.start(context.signal);
  }

  async verifyServices(context) {
    if (!this.service) throw Object.assign(new Error('No supervised backend process is available for health verification.'), { code: 'BACKEND_SERVICE_MISSING' });
    const result = await this.service.verify();
    this.setComponent(context, 'backend_service', { state: 'verified', verification: 'loopback_healthz', last_verified_at: new Date().toISOString(), failure_code: null, message: null, port: result.port });
    const components = context.componentStatus.components;
    const mandatoryVerified = ['uv', 'managed_python', 'backend_environment', 'backend_service', 'silero', 'whisper']
      .every((name) => ['verified', 'files_verified'].includes(components?.[name]?.state));
    this.status(context, { bootstrap_ready: Boolean(context.componentStatus.uv_verified && context.componentStatus.python_verified && context.componentStatus.model_manager_verified),
      partial_ready: mandatoryVerified, production_ready: false });
  }

  async run(stage, context) {
    if (stage === 'start_services') return this.startServices(context);
    if (stage === 'verify_services') return this.verifyServices(context);
    return super.run(stage, context);
  }

  async shutdown() { await this.service?.stop(); }
}
