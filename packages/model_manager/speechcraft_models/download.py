"""Single-stream, resumable, verified installations on local Linux/macOS disks."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .manifest import (ManifestError, load_catalog, select_artifact,
                       verify_artifact, verify_directory_fd)
from .huggingface_source import (HuggingFaceAcquisitionError, PUBLIC_REPOSITORIES,
                                 acquire_public_snapshot, validate_request)

CHUNK = 64 * 1024
CHECKPOINT_BYTES = 4 * 1024 * 1024
CHECKPOINT_SECONDS = 5.0
STAGE_RETENTION_SECONDS = 7 * 24 * 60 * 60
QUARANTINE_RETENTION_SECONDS = 30 * 24 * 60 * 60
RESERVE = 1024 * 1024


class InstallError(RuntimeError):
    """Installation failed; no successful publication is claimed."""


class _Retryable(InstallError):
    pass


def _directory(parent: int, name: str, create: bool = False) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _file(parent: int, name: str, flags: int) -> int:
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise InstallError(f"unsafe file: {name}")
    return fd


def _digest(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    h = hashlib.sha256()
    for chunk in iter(lambda: os.read(fd, CHUNK), b''):
        h.update(chunk)
    return h.hexdigest()


def _sha256():
    return hashlib.sha256()


def _write(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("short write")
        view = view[n:]


def _checkpoint(directory: int, name: str, state: dict) -> None:
    # No fixed temporary filename: an attacker/stale attempt cannot redirect it.
    temp = '.checkpoint-' + uuid.uuid4().hex
    fd = _file(directory, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        _write(fd, json.dumps(state).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


def _remove_tree(parent: int, name: str) -> None:
    """Remove one generated entry without following symlinks or leaving its parent."""
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent)
        return
    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        for entry in os.listdir(child):
            _remove_tree(child, entry)
        os.fsync(child)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)
    os.fsync(parent)


def _sync_tree(directory: int) -> None:
    """Flush a staged regular-file tree without following links."""
    for name in os.listdir(directory):
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _directory(directory, name)
            try:
                _sync_tree(child)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            fd = _file(directory, name, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            raise InstallError(f"unsafe staged entry: {name}")
    os.fsync(directory)


def _strong_etag(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'"[^"\r\n]+"', value) is not None


def _wsl_runtime() -> bool:
    """Detect WSL from the running Linux kernel, independent of model selection."""
    if not sys.platform.startswith('linux'):
        return False
    try:
        release = os.uname().release.lower()
    except (AttributeError, OSError):
        release = ''
    try:
        with open('/proc/sys/kernel/osrelease', encoding='utf-8') as stream:
            release += ' ' + stream.read().lower()
    except OSError:
        pass
    return ('microsoft' in release or 'wsl' in release
            or bool(os.environ.get('WSL_DISTRO_NAME'))
            or bool(os.environ.get('WSL_INTEROP')))


def _mountinfo_path(value: str) -> Path:
    """Decode the octal escapes used by proc mountinfo fields."""
    value = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match.group(1), 8)), value)
    return Path(value)


def _reject_wsl_windows_mount(root: Path, platform: str | None = None) -> None:
    """Reject Windows-backed storage on WSL, regardless of artifact label.

    ``platform`` remains accepted for internal-call compatibility but has no
    bearing on host detection or the storage policy.
    """
    if not _wsl_runtime():
        return
    try:
        candidate = root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InstallError('cannot safely resolve WSL model/cache storage path') from exc
    best = None
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as mounts:
            for line in mounts:
                left, right = line.rstrip().split(' - ', 1)
                fields = left.split()
                if len(fields) < 5:
                    raise ValueError('malformed mountinfo entry')
                mountpoint = _mountinfo_path(fields[4])
                try:
                    candidate.relative_to(mountpoint)
                except ValueError:
                    continue
                if best is None or len(mountpoint.parts) > len(best[0].parts):
                    right_fields = right.split()
                    if not right_fields:
                        raise ValueError('malformed mountinfo filesystem')
                    best = (mountpoint, right_fields[0].lower(), right_fields[1].lower())
    except (OSError, ValueError, IndexError) as exc:
        raise InstallError('cannot confirm WSL model/cache filesystem; choose a path under the WSL Linux filesystem') from exc
    if best is None:
        raise InstallError('cannot confirm WSL model/cache filesystem; choose a path under the WSL Linux filesystem')
    fstype, source = best[1], best[2]
    if fstype in {'9p', 'drvfs'} or 'drvfs' in fstype or source == 'drvfs':
        raise InstallError('WSL model and Hugging Face storage must use the WSL Linux filesystem, not a Windows-mounted filesystem')


def _validate_huggingface_storage(root: Path) -> None:
    """Validate every effective Hub cache location using Hub's own constants."""
    if not _wsl_runtime():
        return
    try:
        from huggingface_hub import constants
    except ImportError as exc:
        raise InstallError('cannot resolve Hugging Face cache locations on WSL') from exc
    # These constants are resolved by huggingface_hub from HF_HOME,
    # HF_HUB_CACHE (including its supported legacy alias), and HF_XET_CACHE.
    # Also check HF_HOME itself because Xet/Hub metadata may use its children.
    locations = {
        'HF_HOME': getattr(constants, 'HF_HOME', None),
        'HF_HUB_CACHE': getattr(constants, 'HF_HUB_CACHE', None),
        'HF_XET_CACHE': getattr(constants, 'HF_XET_CACHE', None),
        'model staging': str(root),
    }
    for label, location in locations.items():
        if not location:
            raise InstallError(f'cannot resolve configured Hugging Face {label} location on WSL')
        try:
            _reject_wsl_windows_mount(Path(location))
        except InstallError as exc:
            raise InstallError(f'unsafe WSL {label} location: {exc}') from exc


def _allocated_bytes_without_symlinks(root: Path) -> int:
    """Measure a cache subtree without traversing symlinks."""
    total = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    total += _allocated_bytes_without_symlinks(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += getattr(info, 'st_blocks', 0) * 512 or info.st_size
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise InstallError('cannot inspect configured Hugging Face Xet cache safely') from exc
    return total


def _xet_chunk_cache_bytes(cache: Path) -> int:
    """Count only Xet download chunk-cache data, not logs or upload shards."""
    total = 0
    try:
        environments = list(os.scandir(cache))
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise InstallError('cannot inspect configured Hugging Face Xet cache safely') from exc
    for environment in environments:
        if environment.is_dir(follow_symlinks=False):
            total += _allocated_bytes_without_symlinks(Path(environment.path) / 'chunk_cache')
    return total


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The first source is immutable raw GitHub, which needs no redirect.
        # Adding a CDN later requires an explicit trusted redirect policy.
        return None


class _Installer:
    """Internal trusted-catalog seam for tests; public API never accepts URLs/catalogs."""
    def __init__(self, root, catalog, *, progress=None, attempts=3, timeout=15,
                 lock_timeout=60, allow_test_http=False, retry_delay=0.25,
                 test_hf_endpoint=None):
        self.root = Path(root).expanduser().absolute()
        self.catalog = catalog
        self.progress = progress or (lambda event: None)
        self.attempts = attempts
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.allow_test_http = allow_test_http
        self.retry_delay = retry_delay
        self.test_hf_endpoint = test_hf_endpoint
        self._event_context = ContextVar(f"speechcraft_model_event_{id(self)}", default={})
        if test_hf_endpoint is not None and not allow_test_http:
            raise ValueError('test Hugging Face endpoint requires the internal test transport flag')
        self.opener = urllib.request.build_opener(_NoRedirect)

    def emit(self, event, **fields):
        context = self._event_context.get()
        default_state = {'installed': 'complete', 'reused': 'complete',
                         'source_acquiring': 'downloading', 'progress': 'downloading'}.get(
                             event, event)
        state = fields.pop('state', default_state)
        self.progress({'event': event, 'state': state, **context, **fields})

    def emit_source(self, event, *, context=None):
        event = dict(event)
        kind = event.pop('event', 'source_progress')
        default_state = {'source_progress': 'downloading',
                         'source_file_acquired': 'downloading'}.get(kind, kind)
        state = event.pop('state', default_state)
        if context is None:
            context = self._event_context.get()
        self.progress({'event': kind, 'state': state, **context, **event})

    def resolve(self, role, platform, *, experimental=False):
        artifact = select_artifact(self.catalog, role, platform)
        model = next(m for m in self.catalog['models'] if m['role'] == role)
        experimental_titanet = (experimental and role == 'speaker_purity'
                                and model['id'] == 'titanet-large-speaker-purity'
                                and artifact.get('source', {}).get('repo_id')
                                == 'csukuangfj/speaker-embedding-models')
        if (artifact['availability'] != 'verified' or not artifact['inventory_complete']
                or not artifact['required_files']
                or artifact.get('release_blockers') and not experimental_titanet):
            raise InstallError('artifact is unresolved or release-blocked')
        if model['license'].get('status') != 'verified' and not experimental_titanet:
            raise InstallError('model licensing is unresolved')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', model['id']):
            raise InstallError('unsafe model id')
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', artifact['revision']):
            raise InstallError('artifact needs an immutable commit or content revision')
        source = artifact.get('source')
        if source is not None:
            try:
                validate_request(source['repo_id'], artifact['revision'],
                                 [item['path'] for item in artifact['required_files']])
            except HuggingFaceAcquisitionError as exc:
                raise InstallError(str(exc)) from exc
        else:
            for file in artifact['required_files']:
                self._validate_http_file(file)
        key = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return model['id'], key, artifact

    def _validate_http_file(self, file):
        if 'sha256' not in file or type(file.get('size_bytes')) is not int or file['size_bytes'] <= 0:
            raise InstallError('every download needs exact positive size and SHA-256')
        url = urllib.parse.urlsplit(file.get('url', ''))
        local_test = self.allow_test_http and url.scheme == 'http' and url.hostname == '127.0.0.1'
        if (not local_test and (url.scheme != 'https' or url.hostname != 'raw.githubusercontent.com')) or url.username or url.password or url.fragment:
            raise InstallError('source is not an approved immutable download URL')

    @contextmanager
    def store(self, model_id, create):
        if create:
            self.root.parent.mkdir(parents=True, exist_ok=True)
        # Parent resolution allows normal OS aliases (/tmp on macOS). The chosen
        # root itself and every component below it must be actual directories.
        parent = os.open(self.root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            root = _directory(parent, self.root.name, create)
        finally:
            os.close(parent)
        try:
            model = _directory(root, model_id, create)
            try:
                yield root, model
            finally:
                os.close(model)
        finally:
            os.close(root)

    def check(self, role, platform, *, experimental=False):
        model_id, key, artifact = self.resolve(role, platform, experimental=experimental)
        path = self.root / model_id / key
        try:
            with self.store(model_id, False) as (_, model):
                final = _directory(model, key)
                try:
                    report = verify_directory_fd(final, artifact, str(path))
                finally:
                    os.close(final)
        except FileNotFoundError:
            return {'status': 'missing', 'install_ready': False, 'runtime_ready': None, 'root': str(path)}
        return report

    @contextmanager
    def lock(self, model, key):
        fd = _file(model, '.lock-' + key, os.O_RDWR | os.O_CREAT)
        deadline = time.monotonic() + self.lock_timeout
        waiting = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        self.emit('waiting_for_lock')
                        waiting = True
                    if time.monotonic() >= deadline:
                        raise InstallError('timed out waiting for installation lock')
                    time.sleep(0.05)
            yield
        finally:
            # Never unlink lock files: replacing their inode would split the lock.
            os.close(fd)

    @contextmanager
    def _try_lock(self, model, key):
        """Yield whether another process is inactive; never wait during cleanup."""
        try:
            fd = _file(model, '.lock-' + key, os.O_RDWR | os.O_CREAT)
        except OSError:
            yield False
            return
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
            yield acquired
        finally:
            os.close(fd)

    def _clean_stage_payloads(self, stage):
        for name in os.listdir(stage):
            if name.startswith('payload-') or name.startswith('.checkpoint-'):
                _remove_tree(stage, name)

    def _cleanup_stages(self, model, artifact_key):
        """Clean only owned stage entries, respecting each artifact's live lock."""
        now = time.time()
        for name in os.listdir(model):
            match = re.fullmatch(r'\.stage-([a-f0-9]{64})', name)
            if not match:
                continue
            key = match.group(1)
            if key == artifact_key:
                # The caller owns this key's lock. An expired same-key attempt
                # is no longer eligible for resume and can be removed safely.
                try:
                    info = os.stat(name, dir_fd=model, follow_symlinks=False)
                    if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                            and not info.st_mode & 0o077
                            and now - info.st_mtime >= STAGE_RETENTION_SECONDS):
                        _remove_tree(model, name)
                except OSError:
                    pass
                continue
            try:
                info = os.stat(name, dir_fd=model, follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077):
                    continue
                with self._try_lock(model, key) as inactive:
                    if not inactive:
                        continue
                    stage = _directory(model, name)
                    try:
                        self._clean_stage_payloads(stage)
                    finally:
                        os.close(stage)
                    # Partial files are retained as resume candidates and
                    # validated before use. Inactive attempts expire after a week.
                    if now - info.st_mtime >= STAGE_RETENTION_SECONDS:
                        _remove_tree(model, name)
            except (OSError, InstallError):
                # Unexpected entries and concurrent filesystem changes are retained.
                continue

    def _cleanup_quarantines(self, model, artifact_key):
        """Expire old quarantine trees only while holding their artifact lock."""
        now = time.time()
        for name in os.listdir(model):
            match = re.fullmatch(r'\.quarantine-([a-f0-9]{64})-([a-f0-9]{32})', name)
            if not match:
                continue
            key = match.group(1)
            if key == artifact_key:
                # The caller owns this artifact lock, so stale diagnostic trees
                # for this artifact are also safe to expire.
                try:
                    info = os.stat(name, dir_fd=model, follow_symlinks=False)
                    if (stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                            and not info.st_mode & 0o077
                            and now - info.st_mtime >= QUARANTINE_RETENTION_SECONDS):
                        _remove_tree(model, name)
                except OSError:
                    pass
                continue
            try:
                info = os.stat(name, dir_fd=model, follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077
                        or now - info.st_mtime < QUARANTINE_RETENTION_SECONDS):
                    continue
                with self._try_lock(model, key) as inactive:
                    if inactive:
                        _remove_tree(model, name)
            except (OSError, InstallError):
                # Unsafe entries and concurrent filesystem changes are retained.
                continue

    def _check_huggingface_capacity(self, root, total_bytes, staged_path=None, artifact=None):
        """Reserve output and transient Xet headroom on each affected volume."""
        from huggingface_hub import constants

        staged_verified_bytes = 0
        try:
            staged_is_dir = stat.S_ISDIR(staged_path.lstat().st_mode) if staged_path is not None else False
        except OSError:
            staged_is_dir = False
        if staged_is_dir and artifact is not None:
            staged = verify_artifact(staged_path, artifact)
            if staged.get('integrity_verified'):
                staged_verified_bytes = total_bytes
            else:
                staged_verified_bytes = sum(
                    item.get('size_bytes', 0) for item in staged.get('files', [])
                    if item.get('status') == 'verified'
                )
        remaining_bytes = max(0, total_bytes - staged_verified_bytes)
        output_required = remaining_bytes + RESERVE
        # hf-xet's chunk cache defaults to disabled. If explicitly enabled,
        # reserve this artifact's possible cache growth up to its configured
        # limit, and credit already allocated download chunks.
        raw_chunk_limit = os.environ.get('HF_XET_CHUNK_CACHE_SIZE_BYTES', '0')
        if re.fullmatch(r'[0-9]+', raw_chunk_limit) is None:
            raise InstallError('HF_XET_CHUNK_CACHE_SIZE_BYTES must be a nonnegative byte count')
        chunk_cache_limit = int(raw_chunk_limit)
        cache = Path(constants.HF_XET_CACHE).expanduser()
        _validate_huggingface_storage(self.root)
        probe = cache
        while not probe.exists():
            if probe.parent == probe:
                raise InstallError('cannot locate Hugging Face Xet cache filesystem')
            probe = probe.parent
        if not probe.is_dir():
            raise InstallError('Hugging Face Xet cache location is not a directory')
        cached_chunks = _xet_chunk_cache_bytes(cache) if chunk_cache_limit else None
        xet_growth_required = max(0, min(chunk_cache_limit, remaining_bytes) - (cached_chunks or 0))
        xet_required = xet_growth_required + RESERVE
        xet_free = shutil.disk_usage(probe).free
        root_stat = os.fstat(root)
        xet_stat = os.stat(probe)
        root_statvfs = os.fstatvfs(root)
        root_free = root_statvfs.f_bavail * root_statvfs.f_frsize
        same_volume = root_stat.st_dev == xet_stat.st_dev
        root_required = output_required + xet_required if same_volume else output_required
        self.emit('capacity', required_bytes=root_required, available_bytes=root_free,
                  artifact_total_bytes=total_bytes, staged_verified_bytes=staged_verified_bytes,
                  estimated_remaining_bytes=remaining_bytes,
                  xet_chunk_cache_limit_bytes=chunk_cache_limit,
                  xet_chunk_cache_used_bytes=cached_chunks,
                  xet_chunk_cache_growth_required_bytes=xet_growth_required,
                  xet_cache_required_bytes=xet_required,
                  xet_cache_available_bytes=xet_free, same_filesystem=same_volume)
        if root_free < root_required:
            raise InstallError(f'insufficient model-store disk space: need {root_required}, available {root_free}')
        if not same_volume and xet_free < xet_required:
            raise InstallError(f'insufficient Hugging Face Xet-cache disk space: need {xet_required}, available {xet_free}')

    def _unlink_hf_file(self, source_fd, relative_path):
        parts = relative_path.split('/')
        directory = os.dup(source_fd)
        try:
            for part in parts[:-1]:
                child = _directory(directory, part)
                os.close(directory)
                directory = child
            try:
                fd = _file(directory, parts[-1], os.O_RDONLY)
            except FileNotFoundError:
                return
            else:
                os.close(fd)
            os.unlink(parts[-1], dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(directory)

    def _acquire_huggingface(self, stage, source_path, artifact):
        source = artifact['source']
        source_name = '.hf-source'
        source_fd = _directory(stage, source_name, True)
        try:
            for attempt in range(1, self.attempts + 1):
                current = verify_directory_fd(source_fd, artifact, str(source_path))
                if current['status'] in ('error',):
                    raise InstallError('unsafe or unreadable Hugging Face staging files')
                for file_result in current['files']:
                    if file_result['status'] in ('corrupt', 'partial'):
                        self._unlink_hf_file(source_fd, file_result['path'])
                self.emit('source_acquiring', source='huggingface', attempt=attempt,
                          files_total=len(artifact['required_files']),
                          total_bytes=sum(item['size_bytes'] for item in artifact['required_files']),
                          transferred_bytes=None, current_file=None)
                try:
                    event_context = self._event_context.get()
                    acquire_public_snapshot(
                        source['repo_id'], artifact['revision'],
                        [item['path'] for item in artifact['required_files']],
                        source_path,
                        lambda event: self.emit_source(event, context=event_context),
                        test_endpoint=self.test_hf_endpoint,
                        total_bytes=sum(item['size_bytes'] for item in artifact['required_files']))
                except HuggingFaceAcquisitionError as exc:
                    if not exc.transient or attempt == self.attempts:
                        raise InstallError(str(exc)) from exc
                    self.emit('retrying', source='huggingface', attempt=attempt, error=str(exc))
                    time.sleep(min(2, self.retry_delay * 2 ** (attempt - 1)))
                    continue
                report = verify_directory_fd(source_fd, artifact, str(source_path))
                if report['integrity_verified']:
                    for file in artifact['required_files']:
                        self.emit('source_file_acquired', source='huggingface',
                                  path=file['path'], size_bytes=file['size_bytes'])
                    return
                if report['status'] in ('error', 'unsafe'):
                    raise InstallError('unsafe or unreadable Hugging Face download result')
                if any(item['status'] in ('corrupt', 'partial') for item in report['files']):
                    for item in report['files']:
                        if item['status'] in ('corrupt', 'partial'):
                            self._unlink_hf_file(source_fd, item['path'])
                    if attempt < self.attempts:
                        self.emit('retrying', source='huggingface', attempt=attempt,
                                  error='downloaded file failed trusted size or SHA-256 verification')
                        continue
                    raise InstallError('Hugging Face files failed trusted size or SHA-256 verification')
                raise InstallError('Hugging Face snapshot is missing required catalog files')
        finally:
            os.close(source_fd)

    def install(self, role, platform, *, experimental=False):
        token = self._event_context.set({'role': role, 'platform': platform})
        self.emit('resolving')
        try:
            return self._install(role, platform, experimental=experimental)
        except (OSError, urllib.error.URLError, http.client.HTTPException, ManifestError, InstallError) as exc:
            self.emit('failed', error=str(exc))
            raise InstallError(str(exc)) from exc
        finally:
            self._event_context.reset(token)

    def _install(self, role, platform, *, experimental=False):
        model_id, key, artifact = self.resolve(role, platform, experimental=experimental)
        _reject_wsl_windows_mount(self.root)
        if artifact.get('source', {}).get('type') == 'huggingface':
            _validate_huggingface_storage(self.root)
        path = self.root / model_id / key
        with self.store(model_id, True) as (root, model), self.lock(model, key):
            self._cleanup_stages(model, key)
            self._cleanup_quarantines(model, key)
            stage_name = '.stage-' + key
            try:
                stage = _directory(model, stage_name)
            except FileNotFoundError:
                stage = None
            if stage is not None:
                try:
                    self._clean_stage_payloads(stage)
                finally:
                    os.close(stage)
            final = None
            try:
                final = _directory(model, key)
            except FileNotFoundError:
                pass
            if final is not None:
                try:
                    report = verify_directory_fd(final, artifact, str(path))
                finally:
                    os.close(final)
                if report['install_ready'] or (experimental and report['integrity_verified']):
                    try:
                        _remove_tree(model, stage_name)
                    except FileNotFoundError:
                        pass
                    self.emit('reused', root=str(path))
                    return {**report, 'reused': True}
                if report['status'] == 'error':
                    raise InstallError('existing installation has unsafe or unreadable files')
            total_bytes = sum(f['size_bytes'] for f in artifact['required_files'])
            if artifact.get('source', {}).get('type') == 'huggingface':
                stage_path = self.root / model_id / stage_name / '.hf-source'
                self._check_huggingface_capacity(root, total_bytes, stage_path, artifact)
            else:
                required = 2 * total_bytes + RESERVE
                fs = os.fstatvfs(root)
                free = fs.f_bavail * fs.f_frsize
                self.emit('capacity', required_bytes=required, available_bytes=free)
                if free < required:
                    raise InstallError(f'insufficient disk space: need {required}, available {free}')
            stage = _directory(model, stage_name, True)
            payload = None
            published = False
            try:
                if artifact.get('source', {}).get('type') == 'huggingface':
                    payload_name = '.hf-source'
                    source_dir = self.root / model_id / stage_name / payload_name
                    self._acquire_huggingface(stage, source_dir, artifact)
                    payload = _directory(stage, payload_name)
                    try:
                        _remove_tree(payload, '.cache')
                    except FileNotFoundError:
                        pass
                else:
                    payload_name = 'payload-' + uuid.uuid4().hex
                    os.mkdir(payload_name, mode=0o700, dir_fd=stage)
                    payload = _directory(stage, payload_name)
                    for index, file in enumerate(artifact['required_files']):
                        self.download_file(stage, index, file)
                        self.materialize(stage, payload, index, file)
                self.emit('verifying')
                report = verify_directory_fd(payload, artifact, str(path))
                if (not report['integrity_verified']
                        or not report['install_ready'] and not experimental):
                    raise InstallError('staged artifact failed verification')
                if artifact.get('source', {}).get('type') == 'huggingface':
                    _sync_tree(payload)
                else:
                    os.fsync(payload)
                self.emit('publishing', root=str(path))
                # Never delete a prior tree. Invalid same-version trees are retained
                # for diagnosis; older valid versions have different keys.
                try:
                    os.stat(key, dir_fd=model, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    os.rename(key, '.quarantine-' + key + '-' + uuid.uuid4().hex,
                              src_dir_fd=model, dst_dir_fd=model)
                    os.fsync(model)
                os.rename(payload_name, key, src_dir_fd=stage, dst_dir_fd=model)
                os.fsync(model)
                published = True
                self.emit('installed', root=str(path), install_ready=report['install_ready'],
                          runtime_ready=None)
                return {**report, 'reused': False}
            finally:
                if payload is not None:
                    os.close(payload)
                os.close(stage)
                if published:
                    try:
                        _remove_tree(model, stage_name)
                    except OSError as exc:
                        self.emit('cleanup_deferred', path=stage_name, error=str(exc))

    def materialize(self, stage, payload, index, file):
        """Copy only manifest-listed, verified transfer files into the final tree."""
        directory = os.dup(payload)
        try:
            parts = file['path'].split('/')
            for part in parts[:-1]:
                child = _directory(directory, part, True)
                os.close(directory)
                directory = child
            # O_TRUNC must occur only after regular-file/hardlink validation.
            source = _file(stage, f'{index}.part', os.O_RDONLY)
            dest = None
            try:
                dest = _file(directory, parts[-1], os.O_WRONLY | os.O_CREAT)
                os.ftruncate(dest, 0)
                for chunk in iter(lambda: os.read(source, CHUNK), b''):
                    _write(dest, chunk)
                os.fsync(dest)
                os.fsync(directory)
            finally:
                os.close(source)
                if dest is not None:
                    os.close(dest)
        finally:
            os.close(directory)

    def download_file(self, stage, index, file):
        fd = _file(stage, f'{index}.part', os.O_RDWR | os.O_CREAT)
        state_name = f'{index}.resume.json'
        try:
            size = os.fstat(fd).st_size
            state = {}
            try:
                state_fd = _file(stage, state_name, os.O_RDONLY)
                try:
                    if os.fstat(state_fd).st_size <= 16 * 1024:
                        state = json.loads(os.read(state_fd, 16 * 1024))
                finally:
                    os.close(state_fd)
            except (FileNotFoundError, ValueError):
                pass
            if not isinstance(state, dict):
                state = {}
            etag = state.get('etag')
            # Reconstruct the incremental digest in one sequential pass. Resume
            # only when durable metadata exactly accounts for every existing byte.
            checkpoint_hash = state.get('sha256')
            checkpoint_size = state.get('size')
            checkpoint_url = state.get('url')
            resumable = (0 < size <= file['size_bytes']
                         and type(checkpoint_size) is int and checkpoint_size == size
                         and isinstance(checkpoint_url, str) and checkpoint_url == file['url']
                         and _strong_etag(etag)
                         and isinstance(checkpoint_hash, str)
                         and re.fullmatch(r'[0-9a-f]{64}', checkpoint_hash) is not None)
            hasher = _sha256()
            if resumable:
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(fd, CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                if hasher.hexdigest() != checkpoint_hash:
                    resumable = False
                elif size == file['size_bytes']:
                    if hasher.hexdigest() == file['sha256']:
                        os.fsync(fd)
                        self.emit('file_verified', path=file['path'], reused=True)
                        return
                    resumable = False
            if not resumable:
                size = 0
                etag = None
                os.ftruncate(fd, 0)
                hasher = _sha256()
            durable_size = size
            durable_etag = etag if resumable else None
            durable_hasher = hasher.copy()
            last_checkpoint = time.monotonic()
            for attempt in range(1, self.attempts + 1):
                headers = {'Accept-Encoding': 'identity', 'User-Agent': 'SpeechCraft-models/0.1'}
                if size:
                    headers.update(Range=f'bytes={size}-', **{'If-Range': etag})
                self.emit('downloading', path=file['path'], attempt=attempt, downloaded_bytes=size, total_bytes=file['size_bytes'])
                try:
                    request = urllib.request.Request(file['url'], headers=headers)
                    with self.opener.open(request, timeout=self.timeout) as response:
                        code = response.status
                        if code == 200:
                            size = 0
                            os.ftruncate(fd, 0)
                            hasher = _sha256()
                            durable_size = 0
                            durable_hasher = hasher.copy()
                            durable_etag = None
                        elif code == 206 and size:
                            expected = f"bytes {size}-{file['size_bytes'] - 1}/{file['size_bytes']}"
                            if (response.headers.get('Content-Range') != expected
                                    or not _strong_etag(etag)
                                    or response.headers.get('ETag') != etag):
                                os.ftruncate(fd, 0)
                                size = durable_size = 0
                                hasher = durable_hasher = _sha256()
                                etag = durable_etag = None
                                raise _Retryable('invalid response range or validator; restarting file')
                        else:
                            os.ftruncate(fd, 0)
                            size = durable_size = 0
                            hasher = durable_hasher = _sha256()
                            etag = durable_etag = None
                            raise _Retryable(f'unexpected response status {code}')
                        if response.headers.get('Content-Encoding', 'identity') != 'identity':
                            raise InstallError('encoded transfer refused')
                        length = response.headers.get('Content-Length')
                        if length is not None and length != str(file['size_bytes'] - size):
                            raise _Retryable('response length mismatch')
                        etag = response.headers.get('ETag')
                        os.lseek(fd, size, os.SEEK_SET)
                        while size < file['size_bytes']:
                            # read1 retains partial bytes from interrupted responses.
                            chunk = response.read1(min(CHUNK, file['size_bytes'] - size))
                            if not chunk:
                                raise _Retryable('transfer ended before expected size')
                            _write(fd, chunk)
                            hasher.update(chunk)
                            size += len(chunk)
                            checkpoint_due = (size - durable_size >= CHECKPOINT_BYTES
                                              or time.monotonic() - last_checkpoint >= CHECKPOINT_SECONDS)
                            if checkpoint_due and _strong_etag(etag):
                                os.fsync(fd)
                                _checkpoint(stage, state_name, {'url': file['url'], 'etag': etag,
                                                              'size': size, 'sha256': hasher.hexdigest()})
                                durable_size = size
                                durable_etag = etag
                                durable_hasher = hasher.copy()
                                last_checkpoint = time.monotonic()
                            self.emit('progress', path=file['path'], downloaded_bytes=size, total_bytes=file['size_bytes'], attempt=attempt)
                        if response.read(1):
                            os.ftruncate(fd, 0)
                            raise InstallError('transfer exceeds expected size')
                    # The incremental hash covers reconstructed durable prefix
                    # plus each new response byte exactly once. Flush before using
                    # it as the authoritative end-to-end digest.
                    os.fsync(fd)
                    if os.fstat(fd).st_size != file['size_bytes'] or hasher.hexdigest() != file['sha256']:
                        os.ftruncate(fd, 0)
                        raise InstallError('download SHA-256 mismatch')
                    _checkpoint(stage, state_name, {'url': file['url'], 'etag': etag,
                                                    'size': size, 'sha256': hasher.hexdigest()})
                    self.emit('file_verified', path=file['path'], reused=False)
                    return
                except urllib.error.HTTPError as exc:
                    code, error = exc.code, str(exc)
                    exc.close()
                    if code == 416:
                        os.ftruncate(fd, 0)
                        size = durable_size = 0
                        hasher = durable_hasher = _sha256()
                        etag = durable_etag = None
                    elif code not in (408, 429, 500, 502, 503, 504):
                        raise InstallError(f'HTTP {code}') from exc
                except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, _Retryable) as exc:
                    error = str(exc)
                # Drop bytes beyond the last data+metadata checkpoint. A crash
                # between flushing a part and replacing its metadata is handled
                # the same way on next invocation: size mismatch forces restart.
                os.ftruncate(fd, durable_size)
                os.fsync(fd)
                size = durable_size
                etag = durable_etag
                hasher = durable_hasher.copy()
                if attempt == self.attempts:
                    raise InstallError(f'transfer failed after {attempt} attempts: {error}')
                self.emit('retrying', path=file['path'], attempt=attempt, error=error)
                time.sleep(min(2, self.retry_delay * 2 ** (attempt - 1)))
        finally:
            os.close(fd)


def install_model(root, role, platform, *, progress=None, experimental=False):
    """Install from the shipped catalog; experimental bypass is TitaNet-only.

    Experimental TitaNet acquisition can verify file bytes while keeping
    ``install_ready`` false until its release blockers are resolved.
    """
    return _Installer(root, load_catalog(), progress=progress).install(
        role, platform, experimental=experimental)


def check_installation(root, role, platform, *, experimental=False):
    """Read-only status of a catalog installation; does not fetch or load models."""
    return _Installer(root, load_catalog()).check(role, platform, experimental=experimental)
