import { BootstrapError, BootstrapTasks, ensureManagedDirectory, fetchArtifact, stageWorkerBuildSources } from './bootstrap.js';
import { PRODUCTION_STAGES, evaluateRuntimeReadiness, hashMatches, runtimeRoots, selectManagedFfmpeg, selectRuntimeProfile } from './runtime-plan.js';
import { BackendService, reconcileOwnedService } from './backend-service.js';
import { inspectHost } from './platform.js';
import fs from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';

async function trustedFileHash(file) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink()) throw new BootstrapError('BOOTSTRAP_FILE_UNSAFE', 'A managed FFmpeg file is not a regular file.');
  const hash = createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return { size: info.size, sha256: hash.digest('hex') };
}

function applyReadiness(components) {
  const readiness = evaluateRuntimeReadiness(components);
  return {
    readiness,
    components: { ...components, dataset_processing: {
      state: readiness.technical_ready ? 'runtime_ready' : 'blocked',
      runtime_ready: readiness.technical_ready,
      verification: readiness.technical_ready ? 'all_required_runtime_components_verified' : 'derived_component_readiness',
      failure_code: readiness.blockers[0]?.code || null,
      message: readiness.technical_ready ? 'Required managed components and inference operations are available.' : readiness.blockers[0]?.message,
      blockers: readiness.blockers,
    } },
  };
}

function managedDirectoryExists(directory) {
  try {
    const info = fs.lstatSync(directory);
    if (info.isSymbolicLink() || !info.isDirectory()) {
      throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'A versioned runtime environment path is unsafe.');
    }
    return true;
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

/** Full setup orchestration. Unsupported recipes are represented as blockers, not successful installs. */
export class RuntimeTasks extends BootstrapTasks {
  constructor(options = {}) {
    super(options);
    this.stages = PRODUCTION_STAGES;
    this.service = null;
    this.store = options.store;
    this.restorePromise = null;
    this.restoreAttemptId = null;
    this.restoreResult = null;
    this.restoreAbortController = null;
    this.stopping = false;
    if (options.environment) this.environment = options.environment;
    if (options.hostInspector) this.hostInspector = options.hostInspector;
  }

  status(context, patch) {
    const derived = applyReadiness(patch.components || context.componentStatus?.components || {});
    super.status(context, { ...patch, components: derived.components,
      technical_processing_ready: derived.readiness.technical_ready,
      release_blockers: derived.readiness.release_blockers, production_ready: derived.readiness.production_ready });
  }

  setComponent(context, name, patch) {
    const current = context.componentStatus || this.initialBootstrapStatus();
    current.components ||= {};
    const next = { ...(current.components[name] || {}), ...patch };
    if (name === 'backend_service' && patch.state !== 'verified') {
      delete next.verification;
      delete next.health_instance_id;
    }
    current.components[name] = next;
    this.status(context, { components: current.components });
  }

  async installCudaCandidate(context) {
    const host = this.environment;
    const recipe = this.runtimeRecipe.worker?.profiles?.['linux-x86_64-cuda'];
    if (host?.profile !== 'linux-x86_64' || context.componentStatus.host_details?.cuda_candidate !== true) return;
    if (recipe?.available !== true) {
      this.setComponent(context, 'worker_cuda_candidate', { state: 'blocked', failure_code: 'cuda_recipe_unavailable',
        message: recipe?.reason || 'No trusted CUDA worker recipe is available; CPU remains selected.' });
      return;
    }
    const project = path.resolve(this.applicationRoot, recipe.project);
    const lock = path.resolve(project, recipe.lockfile);
    const manifest = path.join(project, 'pyproject.toml');
    if (!hashMatches(lock, recipe.lock_sha256) || !hashMatches(manifest, recipe.project_manifest_sha256)) {
      this.setComponent(context, 'worker_cuda_candidate', { state: 'blocked', failure_code: 'cuda_recipe_integrity_failed',
        message: 'The pinned CUDA worker lock or project manifest did not match the trusted runtime catalog.' });
      return;
    }
    const workerSourceManifest = path.join(this.applicationRoot, this.runtimeRecipe.worker.project, 'pyproject.toml');
    if (this.runtimeRecipe.worker.source_manifest_sha256
        && !hashMatches(workerSourceManifest, this.runtimeRecipe.worker.source_manifest_sha256)) {
      throw new BootstrapError('WORKER_SOURCE_INTEGRITY_FAILED', 'The worker source package manifest differs from its trusted runtime recipe.');
    }
    const target = path.join(this.dataDirectory, 'environments', 'worker',
      `v${recipe.environment_version}-${recipe.lock_sha256.slice(0, 16)}-linux-x86_64-cuda`);
    const python = path.join(target, 'bin', 'python');
    const environmentProbe = `import importlib.util,json,sys\nmodules=('torch','torchaudio','ctranslate2','faster_whisper')\nmissing=[name for name in modules if importlib.util.find_spec(name) is None]\nassert not missing,missing\nprint(json.dumps({'kind':'SPEECHCRAFT_CUDA_PACKAGE_ENV_PROBE','version':sys.version.split()[0],'modules':modules}))`;
    const libraryQuery = `import pathlib,sys; r=pathlib.Path(sys.prefix)/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'; print(':'.join(str(p) for p in sorted(r.glob('nvidia/*/lib')) if p.is_dir()))`;
    const probe = `import ctypes,ctypes.util,json,torch,torchaudio,ctranslate2\nlibs={name:ctypes.util.find_library(name) for name in ('cublas','cudnn','cudart')}\nctypes.CDLL('libcublas.so.12')\nok=torch.cuda.is_available()\nif ok:\n x=torch.ones((32,32),device='cuda'); y=x@x; torch.cuda.synchronize(); assert float(y[0,0])==32.0\nassert ctranslate2.get_cuda_device_count()>0\nprint(json.dumps({'kind':'SPEECHCRAFT_CUDA_ENV_PROBE','torch':torch.__version__,'torchaudio':torchaudio.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0),'ctranslate2':ctranslate2.__version__,'ctranslate2_devices':ctranslate2.get_cuda_device_count(),'libraries':libs,'torch_cuda_success':ok}))`;
    try {
      let needsInstallation = !managedDirectoryExists(target);
      if (!needsInstallation) {
        try {
          const existing = await this.runProcess(python, ['-I', '-c', environmentProbe], context, { cwd: project });
          parseProbe(existing, 'SPEECHCRAFT_CUDA_PACKAGE_ENV_PROBE', 'CUDA_ENVIRONMENT_INVALID');
        } catch (error) {
          if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
          await this.quarantine(target, 'invalid-cuda-env');
          needsInstallation = true;
        }
      }
      if (needsInstallation) {
        this.requireSpace((recipe.estimated_bytes || 0) + (this.runtimeRecipe.disk_space.safety_margin_bytes || 0));
        ensureManagedDirectory(this.dataDirectory, target);
        const env = { ...this.makeEnvironment(this.paths), UV_PROJECT_ENVIRONMENT: target };
        this.emit(context, { phase: 'installing', current: null, total: null, percent: null,
          message: 'Installing the pinned CUDA worker candidate; CPU environment remains available.' });
        // Local CUDA dependencies are built from the release worker sources.
        // Keep uv/setuptools writes in managed staging because the installed
        // release is immutable, just as for the ordinary CPU worker profile.
        const stagedProject = stageWorkerBuildSources(this.applicationRoot, this.dataDirectory,
          path.join(this.attemptDirectory(context), 'cuda-candidate'), recipe.project);
        const stagedLock = path.join(stagedProject, path.relative(project, lock));
        if (!hashMatches(stagedLock, recipe.lock_sha256)
            || !hashMatches(path.join(stagedProject, 'pyproject.toml'), recipe.project_manifest_sha256)) {
          throw new BootstrapError('WORKER_SOURCE_STAGING_FAILED', 'The staged CUDA worker recipe does not match its trusted identities.');
        }
        await this.runProcess(this.paths.uvExecutable,
          ['sync', '--locked', '--no-dev', '--no-install-project', '--python', context.managedPython || await this.findPython(context), '--project', stagedProject],
          context, { cwd: stagedProject, env });
      }
      const libraryResult = await this.runProcess(python, ['-I', '-c', libraryQuery], context, { cwd: project });
      const libraryPaths = libraryResult.stdout.trim().split(':').filter((entry) => entry && path.resolve(entry).startsWith(`${path.resolve(target)}${path.sep}`));
      const required = ['libcublas.so.12', 'libcudnn.so.9', 'libcudart.so.12'];
      if (!required.every((name) => libraryPaths.some((directory) => fs.existsSync(path.join(directory, name))))) {
        throw new BootstrapError('CUDA_LIBRARIES_MISSING', 'The pinned CUDA environment is missing a required cuBLAS, cuDNN or CUDA runtime library.');
      }
      const candidateEnv = { ...this.makeEnvironment(this.paths), LD_LIBRARY_PATH: libraryPaths.join(path.delimiter),
        HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1', PYTHONNOUSERSITE: '1' };
      const result = await this.runProcess(python, ['-I', '-c', probe], context, { cwd: project, env: candidateEnv });
      const report = JSON.parse(result.stdout.trim().split(/\r?\n/).at(-1));
      if (report.kind !== 'SPEECHCRAFT_CUDA_ENV_PROBE' || report.torch_cuda_success !== true
          || report.ctranslate2_devices < 1 || !String(report.gpu || '').trim()) {
        throw new BootstrapError('CUDA_ENVIRONMENT_PROBE_FAILED', 'The managed CUDA environment did not pass its PyTorch and CTranslate2 device checks.');
      }
      this.setComponent(context, 'worker_cuda_candidate', { state: 'verified', identity: `${this.runtimeRecipe.worker.distribution}/v${recipe.environment_version}/linux-x86_64-cuda`,
        verification: 'torch_cuda_and_ctranslate2_device_probe', last_verified_at: new Date().toISOString(),
        path: target, library_paths: libraryPaths, device: report.gpu, cuda_runtime: report.cuda,
        pytorch: report.torch, torchaudio: report.torchaudio, ctranslate2: report.ctranslate2,
        failure_code: null, message: 'Candidate libraries and device discovery passed; CUDA remains unselected until actual local Whisper inference passes.' });
    } catch (error) {
      if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
      this.setComponent(context, 'worker_cuda_candidate', { state: 'blocked', failure_code: error.code || 'cuda_candidate_failed',
        message: `${error instanceof BootstrapError ? error.message : `Managed CUDA candidate check failed (${error?.name || 'unexpected error'}).`} The validated CPU worker remains selected.`.slice(0, 320) });
    }
  }

  async verifyExistingWorkerProfile(profile, context) {
    const recipe = this.runtimeRecipe.worker?.profiles?.[profile];
    if (recipe?.available !== true || !(recipe.validated === true || recipe.lock_verified === true)) return null;
    const project = path.resolve(this.applicationRoot, recipe.project || this.runtimeRecipe.worker.project);
    const lock = path.resolve(project, recipe.lockfile || this.runtimeRecipe.worker.lockfile.split('/').at(-1));
    if (!hashMatches(lock, recipe.lock_sha256) || !hashMatches(path.join(project, 'pyproject.toml'), recipe.project_manifest_sha256)) return null;
    const target = this.workerEnvironmentPath(recipe, profile);
    if (!managedDirectoryExists(target)) return null;
    const python = path.join(target, 'bin', 'python');
    const probe = `import importlib.util,json,os,sys\nassert sys.version.split()[0]==${JSON.stringify(recipe.python_version)}\nassert os.path.realpath(sys.prefix)==os.path.realpath(${JSON.stringify(target)})\nbase=os.path.realpath(sys.base_prefix); runtime=os.path.realpath(${JSON.stringify(this.paths.pythonRoot)})\nassert base==runtime or base.startswith(runtime+os.sep),base\nmissing=[name for name in ${JSON.stringify(recipe.validation_modules)} if importlib.util.find_spec(name) is None]\nassert not missing,missing\nprint(json.dumps({'kind':'SPEECHCRAFT_WORKER_ENV_PROBE','profile':${JSON.stringify(profile)},'missing':missing}))`;
    try {
      const result = await this.runProcess(python, ['-I', '-c', probe], context, { cwd: project });
      if (result.code !== 0 || !result.stdout.includes('SPEECHCRAFT_WORKER_ENV_PROBE')) return null;
      let libraryPaths = [];
      if (profile.endsWith('-cuda')) {
        const query = `import pathlib,sys; r=pathlib.Path(sys.prefix)/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/'site-packages'; print(':'.join(str(p) for p in sorted(r.glob('nvidia/*/lib')) if p.is_dir()))`;
        const libraries = await this.runProcess(python, ['-I', '-c', query], context, { cwd: project });
        libraryPaths = libraries.stdout.trim().split(':').filter((entry) => entry
          && path.resolve(entry).startsWith(`${path.resolve(target)}${path.sep}`));
        if (!['libcublas.so.12', 'libcudnn.so.9', 'libcudart.so.12'].every((name) => libraryPaths.some((directory) => fs.existsSync(path.join(directory, name))))) return null;
      }
      return { profile, recipe, target, python, libraryPaths };
    } catch (error) {
      if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
      return null;
    }
  }

  updateServiceStatus(status, record) {
    const current = this.store?.snapshot().job;
    if (!current?.bootstrap_status) return;
    const owned = this.store.snapshot().service_process;
    if (record && owned && (owned.pid !== record.pid || owned.nonce !== record.nonce)) return;
    if (record && !owned && status.state !== 'stopped') return;
    this.store.commit((state) => {
      const components = state.job.bootstrap_status.components || {};
      const next = { ...(components.backend_service || {}), ...status };
      // Health belongs to one supervised process instance. Any transition
      // away from verified invalidates that evidence before readiness is
      // recomputed, so a crash/restart cannot inherit an old green result.
      if (status.state !== 'verified') {
        delete next.verification;
        delete next.health_instance_id;
      }
      components.backend_service = next;
      const derived = applyReadiness(components);
      Object.assign(state.job.bootstrap_status, { components: derived.components,
        technical_processing_ready: derived.readiness.technical_ready,
        release_blockers: derived.readiness.release_blockers, production_ready: derived.readiness.production_ready });
    }, 'backend_service_status');
  }

  async verifyFfmpegDirectory(directory, context, artifact = this.runtimeRecipe.ffmpeg) {
    if (artifact?.acquisition === 'pinned_source_build') return this.verifyBuiltFfmpeg(directory, context, artifact);
    try {
      const identities = [];
      for (const key of ['ffmpeg', 'ffprobe']) {
        const file = path.join(directory, key);
        const expected = artifact.executables[key];
        const identity = await trustedFileHash(file);
        const info = fs.lstatSync(file);
        if ((info.mode & 0o111) === 0 || identity.size !== expected.size_bytes || identity.sha256 !== expected.sha256) return false;
        const result = await this.runProcess(file, ['-version'], context, { cwd: directory });
        const first = result.stdout.split(/\r?\n/)[0] || '';
        const version = first.match(/^ff(?:mpeg|probe) version ([^\s]+)/)?.[1];
        if (!version || version !== artifact.version_identity) return false;
        const config = result.stdout.split(/\r?\n/).find((line) => line.startsWith('configuration:')) || '';
        if (/--enable-(gpl|nonfree)(?:\s|$)/.test(config)) return false;
        identities.push(version);
      }
      return identities[0] === identities[1];
    } catch (error) {
      if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
      return false;
    }
  }

  async verifyBuiltFfmpeg(directory, context, artifact, { requireRecord = true } = {}) {
    try {
      for (const key of ['ffmpeg', 'ffprobe']) {
        const file = path.join(directory, key);
        const info = fs.lstatSync(file);
        if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o111) === 0) return false;
        const result = await this.runProcess(file, ['-version'], context, { cwd: directory });
        const lines = result.stdout.split(/\r?\n/);
        const version = lines[0]?.match(/^ff(?:mpeg|probe) version ([^\s]+)/)?.[1];
        if (!version || version !== artifact.version_identity) return false;
        const config = lines.find((line) => line.startsWith('configuration:')) || '';
        if (/--enable-(gpl|nonfree)(?:\s|$)/.test(config)) return false;
      }
      const recordPath = path.join(directory, 'speechcraft-artifact.json');
      if (!fs.existsSync(recordPath)) return requireRecord ? false : true;
      const record = JSON.parse(fs.readFileSync(recordPath, 'utf8'));
      if (record.schema_version !== 1 || record.acquisition !== 'pinned_source_build' || record.archive_sha256 !== artifact.sha256 || record.source_revision !== artifact.source_revision) return false;
      for (const key of ['ffmpeg', 'ffprobe']) {
        const identity = await trustedFileHash(path.join(directory, key));
        if (identity.sha256 !== record.executables?.[key]?.sha256 || identity.size !== record.executables?.[key]?.size_bytes) return false;
      }
      return true;
    } catch (error) {
      if (['BOOTSTRAP_CANCELLED', 'BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN'].includes(error.code)) throw error;
      return false;
    }
  }

  async installSourceFfmpeg(context, artifact) {
    if (this.environment?.profile !== 'macos-arm64' || artifact.platform !== 'macos-arm64' || artifact.available !== true
        || !/^[a-f0-9]{64}$/.test(artifact.sha256 || '') || !Number.isSafeInteger(artifact.size_bytes)) {
      this.setComponent(context, 'ffmpeg', { state: 'blocked', failure_code: 'runtime_tool_unavailable',
        message: artifact?.reason || 'No pinned Apple Silicon FFmpeg source build is configured.' });
      return false;
    }
    const target = path.join(this.dataDirectory, 'tools', 'ffmpeg', `${artifact.version}-${artifact.sha256.slice(0, 16)}`);
    if (fs.existsSync(target) && await this.verifyFfmpegDirectory(target, context, artifact)) {
      this.setComponent(context, 'ffmpeg', { state: 'files_verified', identity: `ffmpeg-source:${artifact.version}:${artifact.sha256}`,
        verification: 'source_archive_sha256_and_local_build', last_verified_at: new Date().toISOString(), path: target,
        runtime_ready: false, failure_code: null, message: 'The local FFmpeg build matches the pinned source. Decode readiness still requires the runtime probe. This binary is not a redistributed Mac build.' });
      return true;
    }
    if (fs.existsSync(target)) await this.quarantine(target, 'invalid-ffmpeg');
    this.requireSpace(artifact.size_bytes * 8 + (this.runtimeRecipe.disk_space?.safety_margin_bytes || 0));
    const attempt = this.attemptDirectory(context);
    const archive = path.join(attempt, artifact.file);
    const source = path.join(attempt, 'src');
    const prefix = path.join(attempt, 'prefix');
    ensureManagedDirectory(this.dataDirectory, source);
    ensureManagedDirectory(this.dataDirectory, prefix);
    await fetchArtifact(artifact.url, archive, artifact, { signal: context.signal, fetchImpl: this.fetchImpl,
      maxBytes: artifact.size_bytes, onProgress: (progress) => this.emit(context, { ...progress, component: 'ffmpeg', message: 'Downloading the pinned FFmpeg source archive.' }) });
    const python = context.managedPython || await this.findPython(context);
    const extractor = `import pathlib,sys,tarfile\na=pathlib.Path(sys.argv[1]); d=pathlib.Path(sys.argv[2]); root=${JSON.stringify(artifact.tar_root)}\nwith tarfile.open(a,'r:gz') as t:\n for m in t:\n  p=pathlib.PurePosixPath(m.name)\n  if p.is_absolute() or '..' in p.parts or not (m.name==root or m.name.startswith(root+'/')) or not (m.isdir() or m.isfile()): raise SystemExit('unsafe archive member')\n t.extractall(d, filter='data')\n`;
    await this.runProcess(python, ['-I', '-c', extractor, archive, source], context, { cwd: attempt });
    const buildRoot = path.join(source, artifact.tar_root);
    const flags = Array.isArray(artifact.configure) ? artifact.configure : ['--disable-gpl', '--disable-nonfree'];
    await this.runProcess(path.join(buildRoot, 'configure'), [...flags, `--prefix=${prefix}`], context, { cwd: buildRoot });
    await this.runProcess('make', ['-C', buildRoot, '-j2'], context, { cwd: buildRoot });
    await this.runProcess('make', ['-C', buildRoot, 'install'], context, { cwd: buildRoot });
    const stage = path.join(attempt, 'bin');
    ensureManagedDirectory(this.dataDirectory, stage);
    for (const key of ['ffmpeg', 'ffprobe']) {
      fs.copyFileSync(path.join(prefix, 'bin', key), path.join(stage, key));
      fs.chmodSync(path.join(stage, key), 0o755);
    }
    if (!await this.verifyBuiltFfmpeg(stage, context, artifact, { requireRecord: false })) throw new BootstrapError('FFMPEG_EXECUTABLE_INVALID', 'The local FFmpeg build did not match the pinned version or LGPL configuration.');
    const executables = {};
    for (const key of ['ffmpeg', 'ffprobe']) {
      const identity = await trustedFileHash(path.join(stage, key));
      executables[key] = { size_bytes: identity.size, sha256: identity.sha256 };
    }
    fs.writeFileSync(path.join(stage, 'speechcraft-artifact.json'), JSON.stringify({ schema_version: 1, acquisition: 'pinned_source_build',
      version: artifact.version, source_revision: artifact.source_revision, archive_sha256: artifact.sha256, license: artifact.license,
      redistribution_status: artifact.redistribution_status, executables }, null, 2), { mode: 0o600, flag: 'wx' });
    if (!await this.verifyBuiltFfmpeg(stage, context, artifact)) throw new BootstrapError('FFMPEG_EXECUTABLE_INVALID', 'The recorded local FFmpeg build identity did not verify.');
    ensureManagedDirectory(this.dataDirectory, path.dirname(target));
    fs.renameSync(stage, target);
    this.setComponent(context, 'ffmpeg', { state: 'files_verified', identity: `ffmpeg-source:${artifact.version}:${artifact.sha256}`,
      verification: 'source_archive_sha256_and_local_build', last_verified_at: new Date().toISOString(), path: target,
      runtime_ready: false, failure_code: null, message: 'FFmpeg was built from the pinned source on this Mac. Decode probe and redistribution review are still required.' });
    return true;
  }

  async installManagedFfmpeg(context) {
    const artifact = selectManagedFfmpeg(this.runtimeRecipe, this.environment) || this.runtimeRecipe.ffmpeg;
    if (artifact?.acquisition === 'pinned_source_build') return this.installSourceFfmpeg(context, artifact);
    if (artifact?.available !== true || artifact.platform !== this.environment?.profile || !artifact.url || !/^[a-f0-9]{64}$/.test(artifact.sha256 || '')
        || !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes < 1
        || !artifact.executables?.ffmpeg || !artifact.executables?.ffprobe) {
      this.setComponent(context, 'ffmpeg', { state: 'blocked', failure_code: 'runtime_tool_unavailable',
        message: artifact?.reason || 'No verified Linux FFmpeg artifact is configured.' });
      return false;
    }
    const target = path.join(this.dataDirectory, 'tools', 'ffmpeg', `${artifact.version}-${artifact.sha256.slice(0, 16)}`);
    if (fs.existsSync(target) && await this.verifyFfmpegDirectory(target, context)) {
      this.setComponent(context, 'ffmpeg', { state: 'files_verified', identity: `ffmpeg:${artifact.version}:${artifact.sha256}`,
        verification: 'archive_and_executable_sha256', last_verified_at: new Date().toISOString(), path: target,
        runtime_ready: false, failure_code: null, message: 'FFmpeg and FFprobe binaries are verified; audio decode probe is pending.' });
      return true;
    }
    if (fs.existsSync(target)) await this.quarantine(target, 'invalid-ffmpeg');
    this.requireSpace(artifact.size_bytes + (artifact.estimated_install_bytes || artifact.size_bytes * 4)
      + (this.runtimeRecipe.disk_space.safety_margin_bytes || 0));
    const attempt = this.attemptDirectory(context);
    const archive = path.join(attempt, artifact.file);
    const stage = path.join(attempt, `ffmpeg-${artifact.sha256.slice(0, 12)}-${context.attemptId}-${randomUUID()}`);
    if (fs.existsSync(archive)) {
      const info = fs.lstatSync(archive);
      if (!info.isFile() || info.isSymbolicLink()) throw new BootstrapError('BOOTSTRAP_PATH_UNSAFE', 'The staged FFmpeg archive path is unsafe.');
      fs.unlinkSync(archive);
    }
    ensureManagedDirectory(this.dataDirectory, stage);
    this.setComponent(context, 'ffmpeg', { state: 'downloading', identity: `ffmpeg:${artifact.version}`,
      failure_code: null, message: null, runtime_ready: false });
    await fetchArtifact(artifact.url, archive, artifact, { signal: context.signal, fetchImpl: this.fetchImpl,
      maxBytes: artifact.size_bytes, onProgress: (progress) => this.emit(context,
        { ...progress, component: 'ffmpeg', message: 'Downloading the pinned FFmpeg/FFprobe archive.' }) });
    // fetchArtifact streams and verifies the complete digest before returning.
    this.setComponent(context, 'ffmpeg', { state: 'verifying', verification: 'archive_sha256', failure_code: null, message: null });
    const python = context.managedPython || await this.findPython(context);
    const extractor = `import pathlib,sys,tarfile,shutil\na=pathlib.Path(sys.argv[1]); d=pathlib.Path(sys.argv[2]); prefix=${JSON.stringify(artifact.tar_root + '/')}; root=prefix.rstrip('/'); allow={prefix+'bin/ffmpeg':'ffmpeg',prefix+'bin/ffprobe':'ffprobe',prefix+'LICENSE.txt':'COPYING.LGPLv3.txt'}; found=set(); total=0\nwith tarfile.open(a, 'r:xz') as t:\n for m in t:\n  p=pathlib.PurePosixPath(m.name)\n  if p.is_absolute() or '..' in p.parts or (m.name!=root and not m.name.startswith(prefix)) or not (m.isdir() or m.isfile()): raise SystemExit('unsafe archive member')\n  if not m.isdir() and m.size>250000000: raise SystemExit('archive member too large')\n  total+=m.size\n  if total>500000000: raise SystemExit('archive expansion exceeds limit')\n  if m.name in allow:\n   if m.name in found or not m.isfile() or m.size<=0: raise SystemExit('invalid duplicate payload')\n   f=t.extractfile(m)\n   if f is None: raise SystemExit('missing payload')\n   with (d/allow[m.name]).open('xb') as out: shutil.copyfileobj(f,out,1024*1024)\n   found.add(m.name)\nif found!=set(allow): raise SystemExit('archive is incomplete')\n`;
    await this.runProcess(python, ['-I', '-c', extractor, archive, stage], context, { cwd: attempt });
    for (const key of ['ffmpeg', 'ffprobe']) fs.chmodSync(path.join(stage, key), 0o755);
    if (!await this.verifyFfmpegDirectory(stage, context)) throw new BootstrapError('FFMPEG_EXECUTABLE_INVALID', 'Extracted FFmpeg and FFprobe did not match the pinned executable identities.');
    fs.writeFileSync(path.join(stage, 'speechcraft-artifact.json'), JSON.stringify({ schema_version: 1,
      version: artifact.version, source: artifact.release, archive_sha256: artifact.sha256,
      license: artifact.license, source_revision: artifact.source_revision }, null, 2), { mode: 0o600, flag: 'wx' });
    ensureManagedDirectory(this.dataDirectory, path.dirname(target));
    fs.renameSync(stage, target);
    this.setComponent(context, 'ffmpeg', { state: 'files_verified', identity: `ffmpeg:${artifact.version}:${artifact.sha256}`,
      verification: 'archive_and_executable_sha256', last_verified_at: new Date().toISOString(), path: target,
      runtime_ready: false, failure_code: null, message: 'FFmpeg and FFprobe are verified; audio decode probe is pending.' });
    return true;
  }

  async startServices(context) {
    const backend = context.componentStatus.components?.backend_environment;
    if (backend?.state !== 'verified') {
      this.setComponent(context, 'backend_service', { state: 'blocked', failure_code: 'backend_environment_unavailable', message: 'The verified backend environment is unavailable; service startup was skipped.' });
      return;
    }
    const components = context.componentStatus.components || {};
    const prerequisites = ['worker_environment', 'ffmpeg', 'silero', 'whisper', 'pyannote'];
    const missing = prerequisites.filter((name) => {
      const item = components[name];
      return name === 'worker_environment' ? item?.state !== 'verified' : item?.state !== 'runtime_ready';
    });
    if (missing.length) {
      this.setComponent(context, 'backend_service', { state: 'blocked', failure_code: 'runtime_prerequisite_unavailable',
        message: `Backend startup is blocked until these verified runtime components pass: ${missing.join(', ')}.` });
      return;
    }
    if (this.service?.record && this.service.child?.exitCode === null
        && this.service.child?.signalCode === null) {
      try {
        const result = await this.service.verify();
        this.setComponent(context, 'backend_service', { state: 'verified', verification: 'internal_health_identity', last_verified_at: new Date().toISOString(), failure_code: null, message: null, port: result.port });
        return;
      } catch { await this.service.stop(); }
    } else if (this.store?.snapshot().service_process) {
      await reconcileOwnedService(this.store);
    }
    const target = this.backendEnvironmentPath();
    const worker = components.worker_environment;
    const modelRoot = runtimeRoots(this.dataDirectory).models;
    const asr = components.whisper?.state === 'runtime_ready' ? components.whisper.path : null;
    const silero = components.silero?.state === 'runtime_ready' ? components.silero.path : null;
    const pyannote = components.pyannote?.state === 'runtime_ready' ? components.pyannote.path : null;
    const modelPlatform = selectRuntimeProfile(this.environment).modelPlatform;
    const ffmpegDir = components.ffmpeg?.state === 'runtime_ready' ? components.ffmpeg.path : null;
    const workerPython = worker?.state === 'verified' ? path.join(worker.path, 'bin', 'python') : null;
    const appEnvironment = {
      ...this.makeEnvironment(this.paths),
      SPEECHCRAFT_MANAGED_RUNTIME: '1',
      HF_HUB_OFFLINE: '1',
      TRANSFORMERS_OFFLINE: '1',
      HF_HUB_DISABLE_IMPLICIT_TOKEN: '1',
      SPEECHCRAFT_MODEL_ROOT: modelRoot,
      SPEECHCRAFT_MODEL_PLATFORM: modelPlatform,
      SPEECHCRAFT_ASR_MODEL_PATH: asr || '',
      SPEECHCRAFT_SILERO_MODEL_DIR: silero || '',
      SPEECHCRAFT_SILERO_ONNX_PATH: silero ? path.join(silero, 'silero_vad.onnx') : '',
      SPEECHCRAFT_PYANNOTE_COMMUNITY1_PATH: pyannote || '',
      SPEECHCRAFT_FFMPEG_BIN: ffmpegDir ? path.join(ffmpegDir, 'ffmpeg') : '',
      SPEECHCRAFT_FFPROBE_BIN: ffmpegDir ? path.join(ffmpegDir, 'ffprobe') : '',
      SPEECHCRAFT_FFMPEG_DIR: ffmpegDir || '',
      SPEECHCRAFT_ASR_DEVICE: modelPlatform === 'macos-arm64' ? 'mps' : (context.componentStatus.runtime_profile?.accelerator === 'cuda' ? 'cuda' : 'cpu'),
      SPEECHCRAFT_ASR_BACKEND: modelPlatform === 'macos-arm64' ? 'mlx' : 'ctranslate2',
      SPEECHCRAFT_MANAGED_RUNTIME: '1',
      SPEECHCRAFT_PYANNOTE_DEVICE: context.componentStatus.runtime_profile?.pyannote_cuda_validated === true ? 'cuda' : 'cpu',
      SPEECHCRAFT_PYANNOTE_CUDA_VALIDATED: context.componentStatus.runtime_profile?.pyannote_cuda_validated === true ? '1' : '0',
      LD_LIBRARY_PATH: worker?.library_paths?.join(path.delimiter) || '',
    };
    this.service = new BackendService({ store: this.store, dataDirectory: this.dataDirectory,
      applicationRoot: this.applicationRoot, environment: this.environment,
      pythonPath: path.join(target, 'bin', 'python'), environmentVars: appEnvironment,
      attemptId: context.attemptId,
      preferredPort: Number(process.env.SPEECHCRAFT_BACKEND_PORT || 8010),
      workerRoot: path.join(this.applicationRoot, 'workers', 'dataset'),
      workerPython,
      onStatus: (status, record) => this.updateServiceStatus(status, record),
    });
    try {
      await this.service.start(context.signal);
    } catch (error) {
      const code = /^BACKEND_[A-Z0-9_]+$/.test(error?.code || '') ? error.code : 'BACKEND_START_FAILED';
      const message = /^BACKEND_[A-Z0-9_]+$/.test(error?.code || '') && typeof error.message === 'string'
        ? error.message.slice(0, 320)
        : 'The managed backend could not start or pass its loopback health check. Inspect the backend environment and retry.';
      this.setComponent(context, 'backend_service', { state: 'failed', failure_code: code, message });
      throw Object.assign(new Error(message), { name: 'BootstrapError', code });
    }
  }

  async verifyServices(context) {
    if (!this.service) throw Object.assign(new Error('No supervised backend process is available for health verification.'), { code: 'BACKEND_SERVICE_MISSING' });
    const result = await this.service.verify();
    this.setComponent(context, 'backend_service', { state: 'verified', verification: 'internal_health_identity', last_verified_at: new Date().toISOString(), failure_code: null, message: null, port: result.port });
    const components = context.componentStatus.components;
    const bootstrapVerified = ['uv', 'managed_python', 'backend_environment', 'worker_environment', 'backend_service']
      .every((name) => components?.[name]?.state === 'verified');
    const inferenceVerified = ['ffmpeg', 'silero', 'whisper']
      .every((name) => components?.[name]?.state === 'runtime_ready');
    this.status(context, { bootstrap_ready: Boolean(context.componentStatus.uv_verified && context.componentStatus.python_verified && context.componentStatus.model_manager_verified),
      partial_ready: bootstrapVerified && inferenceVerified, production_ready: false });
  }

  /** Revalidate installed artifacts and restore the managed backend without
   * creating a setup job or invoking any download/install operation. */
  restoreManagedServices({ retry = false } = {}) {
    if (this.stopping) return Promise.resolve({ restored: false, reason: 'controller_shutting_down' });
    const job = this.store?.snapshot().job;
    const attemptId = job?.attempt_id || null;
    if (this.restorePromise) return this.restoreAttemptId === attemptId
      ? this.restorePromise : this.restorePromise.then(() => this.restoreManagedServices());
    const backend = job?.bootstrap_status?.components?.backend_service;
    const serviceHealthy = this.service?.child && this.service.child.exitCode === null
      && this.service.child.signalCode === null && backend?.state === 'verified'
      && backend?.verification === 'internal_health_identity';
    if (this.restoreAttemptId === attemptId && this.restoreResult
        && ((this.restoreResult.restored === true && serviceHealthy) || (this.restoreResult.restored !== true && !retry))) {
      return Promise.resolve(this.restoreResult);
    }
    this.restoreAttemptId = attemptId;
    this.restoreAbortController = new AbortController();
    this.restorePromise = this.performManagedServiceRestore(this.restoreAbortController.signal).then((result) => {
      this.restoreResult = result;
      return result;
    }).finally(() => { this.restorePromise = null; this.restoreAbortController = null; });
    return this.restorePromise;
  }

  async performManagedServiceRestore(signal) {
    const state = this.store?.snapshot();
    const job = state?.job;
    if (!state?.onboarding?.completed || job?.state !== 'completed' || job.mode !== 'runtime'
        || job.plan_id !== 'speechcraft-runtime-v1' || !job.bootstrap_status) return { restored: false, reason: 'onboarding_not_complete' };
    const context = { attemptId: job.attempt_id, attempt: job.attempt_count,
      signal, componentStatus: structuredClone(job.bootstrap_status),
      onComponentStatus: (value) => this.store.commit((current) => {
        if (!this.stopping && current.job?.id === job.id && current.job.attempt_id === job.attempt_id) current.job.bootstrap_status = value;
      }, 'startup_component_revalidated'),
      onChildProcess: (record) => this.store.commit((current) => {
        if (!this.stopping && current.job?.id === job.id && current.job.attempt_id === job.attempt_id) current.job.child_process = record;
      }, record ? 'startup_child_started' : 'startup_child_stopped'),
      onProgress: () => {} };
    context.bootstrapStatus = context.componentStatus;
    try {
      this.environment = this.hostInspector ? this.hostInspector() : inspectHost();
      this.paths = this.getPaths(this.environment.profile, { allowMissingManager: true });
      const managerPath = context.componentStatus.components?.model_manager?.path;
      const managedRoot = path.resolve(this.dataDirectory, 'environments');
      if (!managerPath || path.resolve(managerPath) !== managerPath
          || !path.resolve(managerPath).startsWith(`${managedRoot}${path.sep}`)) {
        throw new BootstrapError('MODEL_MANAGER_ENVIRONMENT_UNAVAILABLE', 'The verified model-manager environment path could not be reconstructed; use trusted repair inputs.');
      }
      this.paths.environmentRoot = managerPath;
      const selectedBeforeRestart = job.bootstrap_status.runtime_profile?.workerProfile;
      const baseSelection = selectRuntimeProfile(this.environment);
      this.status(context, { runtime_profile: baseSelection, accelerator: baseSelection.accelerator || 'cpu',
        pyannote_cuda_validated: false, cuda_fallback_reason: selectedBeforeRestart === 'linux-x86_64-cuda'
          ? 'Revalidating the previously selected CUDA profile before backend restoration.' : null });
      const workerProfile = baseSelection.profile === 'macos-arm64' ? 'macos-arm64' : 'linux-x86_64-cpu';
      const restoredWorker = await this.verifyExistingWorkerProfile(workerProfile, context);
      if (restoredWorker) {
        this.setComponent(context, 'worker_environment', { state: 'verified', identity: `${this.runtimeRecipe.worker.distribution}/v${restoredWorker.recipe.environment_version}/${workerProfile}`,
          verification: 'trusted_lock_and_startup_probe', path: restoredWorker.target, effective_profile: workerProfile,
          last_verified_at: new Date().toISOString(), failure_code: null, message: null });
        if (workerProfile === 'linux-x86_64-cpu') {
          this.setComponent(context, 'worker_cpu_fallback', { state: 'verified', identity: `${this.runtimeRecipe.worker.distribution}/v${restoredWorker.recipe.environment_version}/linux-x86_64-cpu`,
            verification: 'trusted_lock_and_startup_probe', path: restoredWorker.target, last_verified_at: new Date().toISOString(), failure_code: null, message: null });
        }
      } else {
        this.setComponent(context, 'worker_environment', { state: 'blocked', path: null, runtime_ready: false,
          failure_code: 'WORKER_ENVIRONMENT_INVALID', message: `The previously verified ${workerProfile} environment is missing or invalid.` });
      }
      if (baseSelection.profile === 'linux-x86_64' && selectedBeforeRestart === 'linux-x86_64-cuda') {
        const cuda = await this.verifyExistingWorkerProfile('linux-x86_64-cuda', context);
        if (cuda) this.setComponent(context, 'worker_cuda_candidate', { state: 'verified',
          identity: `${this.runtimeRecipe.worker.distribution}/v${cuda.recipe.environment_version}/linux-x86_64-cuda`,
          verification: 'trusted_lock_and_startup_probe', last_verified_at: new Date().toISOString(), failure_code: null,
          message: 'Previously selected CUDA environment is installed; CUDA inference is being revalidated.',
          path: cuda.target, library_paths: cuda.libraryPaths });
        else this.setComponent(context, 'worker_cuda_candidate', { state: 'blocked', path: null,
          failure_code: 'CUDA_PROFILE_INVALID', message: 'The previously selected CUDA environment is missing or no longer matches its trusted recipe; CPU fallback will be used if valid.' });
      }
      const ffmpegRecipe = selectManagedFfmpeg(this.runtimeRecipe, this.environment) || this.runtimeRecipe.ffmpeg;
      if (ffmpegRecipe?.available === true) {
        const ffmpegPath = path.join(this.dataDirectory, 'tools', 'ffmpeg', `${ffmpegRecipe.version}-${ffmpegRecipe.sha256.slice(0, 16)}`);
        const valid = await this.verifyFfmpegDirectory(ffmpegPath, context, ffmpegRecipe);
        this.setComponent(context, 'ffmpeg', valid
          ? { state: 'files_verified', path: ffmpegPath, runtime_ready: false, verification: 'archive_and_executable_sha256', failure_code: null, message: 'Managed FFmpeg files were reverified; audio probe is pending.' }
          : { state: 'blocked', path: null, runtime_ready: false, failure_code: 'FFMPEG_ARTIFACT_INVALID', message: 'The managed FFmpeg installation is missing or corrupt; use setup repair.' });
      }
      // Recovery marks model files as verified but deliberately clears runtime
      // claims. These trusted offline probes restore only capabilities that
      // still pass against the existing local files.
      await super.run('validate_runtime', context);
      const latest = this.store.snapshot().job?.bootstrap_status;
      context.componentStatus = latest || context.componentStatus;
      const before = context.componentStatus.components?.backend_service;
      if (before?.state === 'verified') {
        this.store.commit((current) => {
          if (current.job?.id === job.id && current.job.attempt_id === job.attempt_id) {
            current.job.bootstrap_status.components.backend_service = { ...before, state: 'blocked', verification: 'startup_revalidation_pending',
              failure_code: 'BACKEND_RESTART_PENDING', message: 'Managed services are being restored after controller restart.' };
          }
        }, 'backend_restore_started');
      }
      await this.startServices(context);
      const backend = this.store.snapshot().job?.bootstrap_status?.components?.backend_service;
      const ready = backend?.state === 'verified' && backend?.verification === 'internal_health_identity'
        && this.service?.child?.exitCode === null && this.service?.child?.signalCode === null;
      return { restored: ready, reason: ready ? null : 'runtime_prerequisites_unavailable' };
    } catch (error) {
      if (signal.aborted || this.stopping) {
        if (['BOOTSTRAP_PROCESS_TERMINATION_UNCERTAIN', 'BOOTSTRAP_OWNERSHIP_PERSIST_FAILED'].includes(error?.code)) throw error;
        return { restored: false, reason: 'controller_shutting_down' };
      }
      const code = /^([A-Z][A-Z0-9_]{1,79})$/.test(error?.code || '') ? error.code : 'STARTUP_REVALIDATION_FAILED';
      const message = error instanceof BootstrapError ? error.message.slice(0, 320)
        : 'Previously installed runtime components could not be revalidated. Open /setup to inspect or repair the installation.';
      this.setComponent(context, 'backend_service', { state: 'failed', verification: 'startup_revalidation_failed',
        failure_code: code, message });
      return { restored: false, reason: code };
    }
  }

  async run(stage, context) {
    context.componentStatus = context.bootstrapStatus || this.initialBootstrapStatus();
    if (stage === 'start_services') return this.startServices(context);
    if (stage === 'verify_services') return this.verifyServices(context);
    if (stage === 'install_worker_environment') {
      await super.run(stage, context);
      await this.installCudaCandidate(context);
      return;
    }
    return super.run(stage, context);
  }

  async shutdown() {
    this.stopping = true;
    this.restoreAbortController?.abort();
    // Wait for runProcess to terminate/reconcile any owned probe child before
    // stopping the backend or allowing the server to release installation
    // ownership.
    if (this.restorePromise) await this.restorePromise;
    await this.service?.stop();
  }
}
