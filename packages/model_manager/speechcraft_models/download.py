"""Single-stream, resumable, verified installations on local Linux/macOS disks."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .manifest import ManifestError, load_catalog, select_artifact, verify_directory_fd

CHUNK = 64 * 1024
CHECKPOINT_BYTES = 4 * 1024 * 1024
CHECKPOINT_SECONDS = 5.0
STAGE_RETENTION_SECONDS = 7 * 24 * 60 * 60
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


def _strong_etag(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r'"[^"\r\n]+"', value) is not None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The first source is immutable raw GitHub, which needs no redirect.
        # Adding a CDN later requires an explicit trusted redirect policy.
        return None


class _Installer:
    """Internal trusted-catalog seam for tests; public API never accepts URLs/catalogs."""
    def __init__(self, root, catalog, *, progress=None, attempts=3, timeout=15,
                 lock_timeout=60, allow_test_http=False, retry_delay=0.25):
        self.root = Path(root).expanduser().absolute()
        self.catalog = catalog
        self.progress = progress or (lambda event: None)
        self.attempts = attempts
        self.timeout = timeout
        self.lock_timeout = lock_timeout
        self.allow_test_http = allow_test_http
        self.retry_delay = retry_delay
        self.opener = urllib.request.build_opener(_NoRedirect)

    def emit(self, event, **fields):
        self.progress({'event': event, **fields})

    def resolve(self, role, platform):
        artifact = select_artifact(self.catalog, role, platform)
        model = next(m for m in self.catalog['models'] if m['role'] == role)
        if (artifact['availability'] != 'verified' or not artifact['inventory_complete']
                or not artifact['required_files'] or artifact.get('release_blockers')):
            raise InstallError('artifact is unresolved or release-blocked')
        if model['license'].get('status') != 'verified':
            raise InstallError('model licensing is unresolved')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', model['id']):
            raise InstallError('unsafe model id')
        if not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', artifact['revision']):
            raise InstallError('artifact needs an immutable commit or content revision')
        for file in artifact['required_files']:
            if 'sha256' not in file or type(file.get('size_bytes')) is not int or file['size_bytes'] <= 0:
                raise InstallError('every download needs exact positive size and SHA-256')
            url = urllib.parse.urlsplit(file.get('url', ''))
            local_test = self.allow_test_http and url.scheme == 'http' and url.hostname == '127.0.0.1'
            if (not local_test and (url.scheme != 'https' or url.hostname != 'raw.githubusercontent.com')) or url.username or url.password or url.fragment:
                raise InstallError('source is not an approved immutable download URL')
        # Include the whole artifact: revisions with changed inventories never overwrite one another.
        key = hashlib.sha256(json.dumps(artifact, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return model['id'], key, artifact

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

    def check(self, role, platform):
        model_id, key, artifact = self.resolve(role, platform)
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
                    # Partial files are left intact as resume candidates and
                    # validated before reuse. All inactive attempts expire after
                    # a week so abandoned artifact revisions cannot accumulate.
                    if now - info.st_mtime >= STAGE_RETENTION_SECONDS:
                        _remove_tree(model, name)
            except (OSError, InstallError):
                # An unexpected entry or concurrent filesystem change is retained.
                continue

    def install(self, role, platform):
        self.emit('resolving', role=role, platform=platform)
        try:
            return self._install(role, platform)
        except (OSError, urllib.error.URLError, http.client.HTTPException, ManifestError, InstallError) as exc:
            self.emit('failed', error=str(exc))
            raise InstallError(str(exc)) from exc

    def _install(self, role, platform):
        model_id, key, artifact = self.resolve(role, platform)
        path = self.root / model_id / key
        with self.store(model_id, True) as (root, model), self.lock(model, key):
            self._cleanup_stages(model, key)
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
                if report['install_ready']:
                    try:
                        _remove_tree(model, stage_name)
                    except FileNotFoundError:
                        pass
                    self.emit('reused', root=str(path))
                    return {**report, 'reused': True}
                if report['status'] == 'error':
                    raise InstallError('existing installation has unsafe or unreadable files')
            # Reserve transfer plus publication copies, even for resumption; conservative and
            # leaves the previous installation untouched during transfer.
            required = 2 * sum(f['size_bytes'] for f in artifact['required_files']) + RESERVE
            fs = os.fstatvfs(root)
            free = fs.f_bavail * fs.f_frsize
            self.emit('capacity', required_bytes=required, available_bytes=free)
            if free < required:
                raise InstallError(f'insufficient disk space: need {required}, available {free}')
            stage = _directory(model, stage_name, True)
            payload = None
            published = False
            try:
                payload_name = 'payload-' + uuid.uuid4().hex
                os.mkdir(payload_name, mode=0o700, dir_fd=stage)
                payload = _directory(stage, payload_name)
                for index, file in enumerate(artifact['required_files']):
                    self.download_file(stage, index, file)
                    self.materialize(stage, payload, index, file)
                self.emit('verifying')
                report = verify_directory_fd(payload, artifact, str(path))
                if not report['install_ready']:
                    raise InstallError('staged artifact failed verification')
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
                self.emit('installed', root=str(path), runtime_ready=None)
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
            resumable = (0 < size <= file['size_bytes'] and state.get('size') == size
                         and state.get('url') == file['url'] and _strong_etag(etag)
                         and re.fullmatch(r'[0-9a-f]{64}', state.get('sha256', '')))
            hasher = _sha256()
            if resumable:
                os.lseek(fd, 0, os.SEEK_SET)
                while True:
                    chunk = os.read(fd, CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
                if hasher.hexdigest() != state['sha256']:
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


def install_model(root, role, platform, *, progress=None):
    """Install from the shipped catalog. No caller-provided source URLs."""
    return _Installer(root, load_catalog(), progress=progress).install(role, platform)


def check_installation(root, role, platform):
    """Read-only status of a catalog installation; does not fetch or load models."""
    return _Installer(root, load_catalog()).check(role, platform)
