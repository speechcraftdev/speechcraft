"""Offline integration tests: tiny artifacts served only on loopback."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import math
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open
from unittest.mock import patch

from speechcraft_models.download import _Installer, InstallError, _reject_wsl_windows_mount
from speechcraft_models.manifest import ManifestError, load_catalog, select_artifact

DATA = b'fabricated-model-data-' * 4096
CONFIG = b'{}'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        range_header = self.headers.get('Range')
        server.requests.append((self.path, range_header, self.headers.get('If-Range')))
        mode = server.modes.pop(0) if server.modes else 'ok'
        if mode == 'slow':
            time.sleep(0.2)
        if mode in ('503', '404', '416'):
            self.send_error(int(mode))
            return
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', server.base + '/unapproved')
            self.end_headers()
            return
        data = CONFIG if self.path == '/config' else DATA
        data = server.payloads.get(self.path, data)
        start = int(range_header.split('=')[1].split('-')[0]) if range_header else 0
        resume = bool(range_header) and mode != 'ignore_range'
        if not resume:
            start = 0
        self.send_response(206 if resume else 200)
        self.send_header('Content-Length', str(len(data) - start))
        if mode != 'no_etag':
            self.send_header('ETag', '"different"' if mode == 'bad_etag' else '"fixture-v1"')
        if resume:
            content_range = f'bytes {start}-{len(data)-1}/{len(data)}'
            if mode == 'bad_start':
                content_range = f'bytes {start+1}-{len(data)-1}/{len(data)}'
            if mode == 'bad_total':
                content_range = f'bytes {start}-{len(data)-1}/{len(data)+1}'
            if mode == 'bad_end':
                content_range = f'bytes {start}-{len(data)-2}/{len(data)}'
            if mode == 'bad_range':
                content_range = 'not a range'
            if mode != 'missing_range':
                self.send_header('Content-Range', content_range)
        self.end_headers()
        try:
            if mode in ('drop', 'no_etag'):
                self.wfile.write(data[start:start+1000])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
            else:
                self.wfile.write(b'x' * (len(data) - start) if mode == 'corrupt' else data[start:])
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeHFHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _file(self):
        prefix = f'/{self.server.repo_id}/resolve/'
        path = urllib.parse.unquote(self.path)
        if not path.startswith(prefix):
            return None
        tail = path[len(prefix):].split('/', 1)
        return tail[1] if len(tail) == 2 and tail[0] == self.server.revision else None

    def do_GET(self):
        self.server.auth_headers.append(self.headers.get('Authorization'))
        if self.path.startswith(f'/api/models/{self.server.repo_id}/revision/'):
            body = json.dumps({
                '_id': self.server.repo_id,
                'id': self.server.repo_id,
                'sha': self.server.revision,
                'siblings': [{'rfilename': name} for name in self.server.files],
            }).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        name = self._file()
        data = self.server.files.get(name)
        if data is None:
            self.send_error(404)
            return
        self.server.file_requests.append(name)
        start = 0
        requested = self.headers.get('Range')
        if requested:
            start = int(requested.split('=')[1].split('-')[0])
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{len(data)-1}/{len(data)}')
        else:
            self.send_response(200)
        self.send_header('ETag', '"' + hashlib.sha256(data).hexdigest() + '"')
        self.send_header('X-Repo-Commit', self.server.revision)
        self.send_header('Content-Length', str(len(data) - start))
        self.end_headers()
        if name == self.server.drop_file and not self.server.dropped:
            self.server.dropped = True
            self.wfile.write(data[start:start + max(1, len(data) // 2)])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.close_connection = True
            return
        body = self.server.corrupt_data.get(name, data)
        if name in self.server.corrupt_once:
            self.server.corrupt_once.remove(name)
            body = b'x' * len(data)
        try:
            self.wfile.write(body[start:])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.server.auth_headers.append(self.headers.get('Authorization'))
        name = self._file()
        data = self.server.files.get(name)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('ETag', '"' + hashlib.sha256(data).hexdigest() + '"')
        self.send_header('X-Repo-Commit', self.server.revision)
        self.send_header('X-Linked-Size', str(len(data)))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()


def process_install(root, catalog, queue, crash=False):
    if crash:
        from speechcraft_models import download
        download.CHECKPOINT_BYTES = 512
    def progress(event):
        if crash and event['event'] == 'progress':
            os._exit(37)
    try:
        result = _Installer(root, catalog, allow_test_http=True, retry_delay=0, progress=progress).install('speech_detection', 'linux-x86_64')
        queue.put(('ok', result['reused']))
    except Exception as exc:
        queue.put(('error', str(exc)))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'models'
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.modes = []
        self.server.requests = []
        self.server.payloads = {}
        self.server.base = f'http://127.0.0.1:{self.server.server_port}'
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.catalog = load_catalog()
        self.hf_counter = 0
        self.model = next(m for m in self.catalog['models'] if m['role'] == 'speech_detection')
        self.artifact = self.model['artifacts'][0]
        self.artifact['required_files'] = [
            {'path': 'nested/model.onnx', 'size_bytes': len(DATA), 'sha256': hashlib.sha256(DATA).hexdigest(), 'url': self.server.base + '/model'},
            {'path': 'config.json', 'size_bytes': len(CONFIG), 'sha256': hashlib.sha256(CONFIG).hexdigest(), 'url': self.server.base + '/config'},
        ]
        self.events = []
        self.installer = self.new_installer()

    def new_installer(self, **kwargs):
        return _Installer(self.root, self.catalog, allow_test_http=True, retry_delay=0, progress=self.events.append, **kwargs)

    def install(self):
        return self.installer.install('speech_detection', 'linux-x86_64')

    def stage(self):
        model_id, key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        return self.root / model_id / ('.stage-' + key)

    def final(self):
        model_id, key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        return self.root / model_id / key

    def make_hf_installer(self, files, *, revision='a' * 40, missing_from_repo=(),
                          extra_repo_files=None, role='speaker_purity', platform='linux-x86_64',
                          repo='Wespeaker/wespeaker-ecapa-tdnn512-LM'):
        try:
            from huggingface_hub import constants
            from speechcraft_models import huggingface_source
        except ImportError:
            self.skipTest('install the model-manager Hugging Face test dependencies')
        payloads = dict(files)
        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeHFHandler)
        server.repo_id = repo
        server.revision = revision
        server.files = {name: data for name, data in payloads.items() if name not in missing_from_repo}
        server.files.update(extra_repo_files or {})
        server.file_requests = []
        server.auth_headers = []
        server.corrupt_data = {}
        server.corrupt_once = set()
        server.drop_file = None
        server.dropped = False
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        allowlist = patch.dict(huggingface_source.PUBLIC_REPOSITORIES,
                               {repo: frozenset(payloads)})
        allowlist.start()
        self.addCleanup(allowlist.stop)
        cache = Path(self.temp.name) / 'hf-xet-cache'
        cache.mkdir(exist_ok=True)
        cache_patch = patch.object(constants, 'HF_XET_CACHE', str(cache))
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

        catalog = copy.deepcopy(load_catalog())
        model = next(item for item in catalog['models'] if item['role'] == role)
        artifact = select_artifact(catalog, role, platform)
        artifact['revision'] = revision
        artifact['source'] = {'type': 'huggingface', 'repo_id': repo}
        artifact['required_files'] = [
            {'path': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            for name, data in payloads.items()
        ]
        events = []
        root = Path(self.temp.name) / f'hf-models-{self.hf_counter}'
        self.hf_counter += 1
        installer = _Installer(root, catalog, allow_test_http=True,
                               test_hf_endpoint=f'http://127.0.0.1:{server.server_port}',
                               retry_delay=0, attempts=2, progress=events.append)
        return installer, model, artifact, server, root, events

    def test_clean_install_reuse_and_status(self):
        self.assertEqual(self.installer.check('speech_detection', 'linux-x86_64')['status'], 'missing')
        self.assertFalse(self.root.exists())
        result = self.install()
        self.assertTrue(result['install_ready'])
        self.assertIsNone(result['runtime_ready'])
        self.assertEqual((Path(result['root']) / 'nested/model.onnx').read_bytes(), DATA)
        self.assertEqual(self.installer.check('speech_detection', 'linux-x86_64')['status'], 'verified')
        count = len(self.server.requests)
        self.assertTrue(self.install()['reused'])
        self.assertEqual(len(self.server.requests), count)
        self.assertIn('progress', [e['event'] for e in self.events])
        transfer = [event for event in self.events if event['event'] == 'progress']
        expected_total = sum(item['size_bytes'] for item in self.artifact['required_files'])
        self.assertTrue(transfer)
        self.assertEqual(transfer[-1]['total_bytes'], expected_total)
        self.assertEqual(transfer[-1]['downloaded_bytes'], expected_total)
        self.assertEqual(transfer[-1]['files_total'], len(self.artifact['required_files']))
        self.assertIn(transfer[-1]['path'], {item['path'] for item in self.artifact['required_files']})
        self.assertGreater(transfer[-1]['transfer_attempt_bytes'], 0)

    def test_shared_community1_artifact_reuses_verified_linux_only_key(self):
        catalog = copy.deepcopy(load_catalog())
        model = next(item for item in catalog['models'] if item['role'] == 'diarization')
        artifact = model['artifacts'][0]
        artifact.pop('source', None)
        artifact['required_files'] = [{'path': 'config.yaml', 'size_bytes': 2,
            'sha256': hashlib.sha256(b'{}').hexdigest(), 'url': self.server.base + '/config'}]
        installer = _Installer(self.root, catalog, allow_test_http=True, retry_delay=0)
        model_id, current_key, selected = installer.resolve('diarization', 'macos-arm64')
        legacy_key, = installer._legacy_shared_keys(model_id, selected)
        legacy_dir = self.root / model_id / legacy_key
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'config.yaml').write_bytes(b'{}')
        checked = installer.check('diarization', 'macos-arm64')
        self.assertTrue(checked['install_ready'])
        self.assertEqual(Path(checked['root']), legacy_dir)
        result = installer.install('diarization', 'macos-arm64')
        self.assertTrue(result['reused'])
        self.assertTrue(result['legacy_key_reused'])
        self.assertNotEqual(current_key, legacy_key)

    def test_interruption_resumes_with_strong_validator(self):
        self.server.modes = ['drop', 'ok']
        with patch('speechcraft_models.download.CHECKPOINT_BYTES', 512):
            self.assertTrue(self.install()['install_ready'])
        self.assertEqual(self.server.requests[1][1:], ('bytes=1000-', '"fixture-v1"'))
        resumed = [event for event in self.events if event.get('event') == 'downloading' and event.get('attempt') == 2]
        self.assertTrue(resumed)
        self.assertEqual(resumed[0]['downloaded_bytes'], 1000, 'the verified durable prefix remains visible')
        self.assertEqual(resumed[0]['transfer_attempt_bytes'], 0, 'resumed cache bytes are excluded from new transfer speed')

    def test_server_ignoring_range_restarts(self):
        self.server.modes = ['drop', 'ignore_range']
        with patch('speechcraft_models.download.CHECKPOINT_BYTES', 512):
            self.assertTrue(self.install()['install_ready'])
        self.assertEqual((self.final() / 'nested/model.onnx').read_bytes(), DATA)

    def test_invalid_ranges_restart_without_appending(self):
        for mode in ('bad_start', 'bad_total', 'bad_end', 'bad_range', 'missing_range', 'bad_etag', '416'):
            with self.subTest(mode=mode):
                self.root = Path(self.temp.name) / mode
                self.installer = self.new_installer()
                self.server.requests.clear()
                self.server.modes = ['drop', mode, 'ok']
                with patch('speechcraft_models.download.CHECKPOINT_BYTES', 512):
                    self.assertTrue(self.install()['install_ready'])
                self.assertIsNone(self.server.requests[2][1])

    def test_no_validator_restarts_file(self):
        self.server.modes = ['no_etag', 'ok']
        self.assertTrue(self.install()['install_ready'])
        self.assertIsNone(self.server.requests[1][1])

    def test_retries_are_bounded_and_404_is_not_retried(self):
        self.server.modes = ['503'] * 10
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(len(self.server.requests), 3)
        self.server.requests.clear()
        self.server.modes = ['404']
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(len(self.server.requests), 1)
        self.assertFalse(self.final().exists())

    def test_corrupt_download_never_publishes(self):
        self.server.modes = ['corrupt']
        with self.assertRaisesRegex(InstallError, 'SHA-256'):
            self.install()
        self.assertFalse(self.final().exists())
        self.assertTrue(self.install()['install_ready'])

    def test_corrupt_partial_is_not_resumed_by_size(self):
        self.server.modes = ['drop']
        with patch('speechcraft_models.download.CHECKPOINT_BYTES', 512):
            with self.assertRaises(InstallError):
                self.new_installer(attempts=1).install('speech_detection', 'linux-x86_64')
        partial = self.stage() / '0.part'
        self.assertEqual(partial.stat().st_size, 1000)
        partial.write_bytes(b'x' * 1000)
        self.server.requests.clear()
        self.assertTrue(self.install()['install_ready'])
        self.assertIsNone(self.server.requests[0][1])

    def test_full_sized_corrupt_staging_is_not_reused(self):
        stage = self.stage()
        stage.mkdir(parents=True)
        (stage / '0.part').write_bytes(b'x' * len(DATA))
        self.assertTrue(self.install()['install_ready'])
        self.assertIsNone(self.server.requests[0][1])

    def test_missing_hash_and_unresolved_metadata_refused_before_transfer(self):
        cases = [('availability', 'unresolved'), ('inventory_complete', False), ('release_blockers', ['blocked'])]
        for key, value in cases:
            saved = self.artifact.get(key)
            self.artifact[key] = value
            with self.assertRaises(InstallError):
                self.install()
            self.artifact[key] = saved
        del self.artifact['required_files'][0]['sha256']
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(self.server.requests, [])
        self.assertFalse(self.root.exists())

    def test_insufficient_space(self):
        with patch('speechcraft_models.download.os.fstatvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
            with self.assertRaisesRegex(InstallError, 'disk space'):
                self.install()
        self.assertEqual(self.server.requests, [])

    def test_two_processes_share_one_install(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        self.server.modes = ['slow']
        processes = [ctx.Process(target=process_install, args=(str(self.root), self.catalog, queue)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.kill()
                self.fail('installation deadlocked')
            self.assertEqual(process.exitcode, 0)
        results = [queue.get(timeout=3) for _ in processes]
        queue.close()
        self.assertCountEqual(results, [('ok', False), ('ok', True)])
        self.assertEqual(len(self.server.requests), 2)

    def test_process_crash_releases_lock_and_recovers(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        process = ctx.Process(target=process_install, args=(str(self.root), self.catalog, queue, True))
        process.start()
        process.join(15)
        if process.is_alive():
            process.kill()
            self.fail('crash test hung')
        queue.close()
        self.assertEqual(process.exitcode, 37)
        self.assertFalse(self.final().exists())
        self.assertTrue(self.stage().is_dir())
        self.assertTrue(self.install()['install_ready'])

    def test_failed_new_revision_preserves_old_valid_installation(self):
        old = self.install()
        old_catalog = copy.deepcopy(self.catalog)
        self.artifact['revision'] = 'a' * 40
        self.server.modes = ['corrupt']
        with self.assertRaises(InstallError):
            self.install()
        checker = _Installer(self.root, old_catalog, allow_test_http=True)
        self.assertTrue(checker.check('speech_detection', 'linux-x86_64')['install_ready'])
        self.assertEqual((Path(old['root']) / 'nested/model.onnx').read_bytes(), DATA)

    def test_corrupt_installation_repaired_with_quarantine(self):
        result = self.install()
        (Path(result['root']) / 'nested/model.onnx').write_bytes(b'x' * len(DATA))
        self.assertEqual(self.installer.check('speech_detection', 'linux-x86_64')['status'], 'corrupt')
        self.assertTrue(self.install()['install_ready'])
        self.assertTrue(list(self.final().parent.glob('.quarantine-*')))

    def test_malformed_checkpoint_fields_restart_without_range(self):
        valid = {'url': self.artifact['required_files'][0]['url'], 'etag': '"fixture-v1"',
                 'size': 1000, 'sha256': hashlib.sha256(DATA[:1000]).hexdigest()}
        cases = []
        for field, values in {
            'sha256': [None, 5, [], 'bad'],
            'size': [None, True, [], 1001],
            'etag': [None, 5, [], 'W/"weak"'],
            'url': [None, 5, []],
        }.items():
            for value in values:
                state = dict(valid)
                state[field] = value
                cases.append((f'{field}={value!r}', json.dumps(state)))
        missing = dict(valid)
        del missing['sha256']
        cases.append(('missing hash', json.dumps(missing)))
        for field in ('url', 'etag', 'size', 'sha256'):
            state = dict(valid)
            del state[field]
            cases.append((f'missing {field}', json.dumps(state)))
        cases.extend((('malformed json', '{'), ('oversized', 'x' * 17000)))
        for index, (label, encoded) in enumerate(cases):
            with self.subTest(checkpoint=label):
                self.root = Path(self.temp.name) / f'case-{index}'
                self.installer = self.new_installer()
                stage = self.stage()
                stage.mkdir(parents=True, mode=0o700)
                (stage / '0.part').write_bytes(DATA[:1000])
                (stage / '0.resume.json').write_text(encoded)
                self.server.requests.clear()
                self.server.modes = ['ok']
                self.assertTrue(self.install()['install_ready'])
                self.assertIsNone(self.server.requests[0][1])

    def test_quarantine_cleanup_is_bounded_safe_and_lock_aware(self):
        self.assertTrue(self.install()['install_ready'])
        self.assertEqual(self.installer.check('speech_detection', 'linux-x86_64')['status'], 'verified')
        model_id, current_key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        model_path = self.root / model_id
        stale_key, active_key, recent_key = 'a' * 64, 'b' * 64, 'c' * 64
        old_name = f'.quarantine-{stale_key}-' + '1' * 32
        current_name = f'.quarantine-{current_key}-' + '5' * 32
        active_name = f'.quarantine-{active_key}-' + '2' * 32
        recent_name = f'.quarantine-{recent_key}-' + '3' * 32
        for name in (old_name, current_name, active_name, recent_name):
            (model_path / name).mkdir(mode=0o700)
        old_time = time.time() - 31 * 86400
        os.utime(model_path / old_name, (old_time, old_time))
        os.utime(model_path / current_name, (old_time, old_time))
        os.utime(model_path / active_name, (old_time, old_time))
        outside = Path(self.temp.name) / 'outside-quarantine'
        outside.mkdir()
        unsafe = model_path / (f'.quarantine-{"d" * 64}-' + '4' * 32)
        unsafe.symlink_to(outside, target_is_directory=True)
        model_fd = os.open(model_path, os.O_RDONLY | os.O_DIRECTORY)
        lock_fd = os.open('.lock-' + active_key, os.O_CREAT | os.O_RDWR, 0o600, dir_fd=model_fd)
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.installer._cleanup_quarantines(model_fd, current_key)
            self.assertFalse((model_path / old_name).exists())
            self.assertFalse((model_path / current_name).exists())
            self.assertTrue((model_path / active_name).exists())
            self.assertTrue((model_path / recent_name).exists())
            self.assertTrue(unsafe.is_symlink())
            self.assertTrue(self.installer.check('speech_detection', 'linux-x86_64')['install_ready'])
        finally:
            os.close(lock_fd)
            os.close(model_fd)

    def test_huggingface_install_uses_pinned_allowlist_and_reuses_verified_files(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        data = b'fixture-ecapa-model'
        installer, _, _, server, _, events = self.make_hf_installer(
            {filename: data}, extra_repo_files={'unlisted/other-model.onnx': b'not requested'})
        with patch.dict(os.environ, {'HF_TOKEN': 'must-not-be-used'}):
            result = installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertTrue(result['integrity_verified'])
        self.assertTrue(result['install_ready'])
        self.assertIsNone(result['runtime_ready'])
        self.assertEqual((Path(result['root']) / filename).read_bytes(), data)
        self.assertEqual(set(path.name for path in Path(result['root']).iterdir()), {filename})
        self.assertEqual(server.file_requests, [filename])
        self.assertTrue(all(value is None for value in server.auth_headers))
        source_progress = [event for event in events if event['event'] == 'source_progress']
        self.assertEqual(source_progress[-1]['files_completed'], 1)
        self.assertEqual(source_progress[-1]['files_total'], 1)
        count = len(server.file_requests)
        self.assertTrue(installer.install('speaker_purity', 'linux-x86_64', experimental=True)['reused'])
        self.assertEqual(len(server.file_requests), count)
        self.assertNotIn('must-not-be-used', json.dumps(events))
        self.assertNotIn('http://', json.dumps(events))

    def test_huggingface_requires_full_revision_and_exact_repository_allowlist(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        installer, _, artifact, server, _, _ = self.make_hf_installer({filename: b'x'})
        artifact['revision'] = 'main'
        with self.assertRaises((InstallError, ManifestError)):
            installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        artifact['revision'] = 'a' * 40
        artifact['source']['repo_id'] = 'user/untrusted-model'
        with self.assertRaises(InstallError):
            installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertEqual(server.file_requests, [])

    def test_huggingface_missing_file_and_corrupt_content_never_publish(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        data = b'correct-model-file'
        installer, _, _, server, missing_root, _ = self.make_hf_installer(
            {filename: data}, missing_from_repo={filename})
        with self.assertRaises(InstallError):
            installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertFalse(list(missing_root.glob('**/voxceleb_ECAPA512_LM.onnx')))

        installer, _, _, server, corrupt_root, _ = self.make_hf_installer({filename: data})
        server.corrupt_data[filename] = b'x' * len(data)
        with self.assertRaisesRegex(InstallError, 'SHA-256'):
            installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertFalse(list(corrupt_root.glob('**/voxceleb_ECAPA512_LM.onnx')))
        server.corrupt_data.clear()
        self.assertTrue(installer.install('speaker_purity', 'linux-x86_64', experimental=True)['integrity_verified'])

    def test_huggingface_multifile_completeness_and_interrupted_retry(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        second = 'config/model.json'
        files = {filename: b'weights', second: b'{"fixture":true}'}
        installer, _, _, server, _, _ = self.make_hf_installer(files)
        server.drop_file = filename
        report = installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertTrue(report['integrity_verified'])
        self.assertEqual((Path(report['root']) / second).read_bytes(), files[second])
        self.assertGreaterEqual(server.file_requests.count(filename), 2)
        self.assertCountEqual(server.file_requests, [filename, filename, second])

        installer, _, _, _, missing_root, _ = self.make_hf_installer(files, missing_from_repo={second})
        with self.assertRaises(InstallError):
            installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertFalse(list(missing_root.glob('wespeaker-ecapa-tdnn512-lm/*/voxceleb_ECAPA512_LM.onnx')))

    def test_whisper_multifile_interruption_restarts_and_verifies_every_file(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'preprocessor_config.json': b'{"fixture":"metadata"}',
            'model.bin': b'W' * (256 * 1024),
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, _, server, root, events = self.make_hf_installer(
            files, role='asr', repo=repo, platform='linux-x86_64')
        installer.attempts = 1
        server.drop_file = 'model.bin'
        with self.assertRaises(InstallError):
            installer.install('asr', 'linux-x86_64')
        self.assertFalse(any(event['event'] == 'installed' for event in events))
        stage = next(root.glob('whisper-large-v3/.stage-*'))
        self.assertTrue((stage / '.hf-source/config.json').is_file())
        self.assertTrue(list((stage / '.hf-source/.cache/huggingface/download').glob('*.incomplete')))

        server.drop_file = None
        installer.attempts = 2
        result = installer.install('asr', 'linux-x86_64')
        self.assertTrue(result['install_ready'])
        self.assertEqual({item['path'] for item in result['files']}, set(files))
        for name, data in files.items():
            self.assertEqual((Path(result['root']) / name).read_bytes(), data)
        self.assertTrue(installer.install('asr', 'linux-x86_64')['reused'])
        self.assertEqual(server.file_requests.count('config.json'), 1)
        self.assertGreaterEqual(server.file_requests.count('model.bin'), 2)

    def test_whisper_same_size_corruption_is_retried_before_publication(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'preprocessor_config.json': b'{"fixture":"metadata"}',
            'model.bin': b'W' * (64 * 1024),
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, _, server, _, events = self.make_hf_installer(
            files, role='asr', repo=repo, platform='linux-x86_64')
        server.corrupt_once.add('model.bin')
        result = installer.install('asr', 'linux-x86_64')
        self.assertTrue(result['install_ready'])
        self.assertEqual((Path(result['root']) / 'model.bin').read_bytes(), files['model.bin'])
        self.assertTrue(any(event['event'] == 'retrying' for event in events))
        self.assertTrue(all(item['status'] == 'verified' for item in result['files']))

    def test_whisper_concurrent_same_artifact_installs_serialize(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'preprocessor_config.json': b'{"fixture":"metadata"}',
            'model.bin': b'W' * (32 * 1024),
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, _, server, _, _ = self.make_hf_installer(
            files, role='asr', repo=repo, platform='linux-x86_64')
        errors, results = [], []
        barrier = threading.Barrier(2)
        def run():
            try:
                barrier.wait()
                results.append(installer.install('asr', 'linux-x86_64'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertCountEqual([result['reused'] for result in results], [False, True])
        self.assertCountEqual(server.file_requests, list(files))

    def test_whisper_failed_replacement_preserves_previous_verified_artifact(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'preprocessor_config.json': b'{"fixture":"metadata"}',
            'model.bin': b'W' * (32 * 1024),
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, artifact, server, root, _ = self.make_hf_installer(
            files, role='asr', repo=repo, platform='linux-x86_64')
        previous = installer.install('asr', 'linux-x86_64')
        old_catalog = copy.deepcopy(installer.catalog)
        artifact['revision'] = 'b' * 40
        replacement = b'N' * len(files['model.bin'])
        server.files['model.bin'] = replacement
        for item in artifact['required_files']:
            if item['path'] == 'model.bin':
                item['sha256'] = hashlib.sha256(replacement).hexdigest()
        server.corrupt_data['model.bin'] = b'X' * len(replacement)
        with self.assertRaises(InstallError):
            installer.install('asr', 'linux-x86_64')
        old_checker = _Installer(root, old_catalog, allow_test_http=True,
                                 test_hf_endpoint=installer.test_hf_endpoint)
        self.assertTrue(old_checker.check('asr', 'linux-x86_64')['install_ready'])
        self.assertEqual((Path(previous['root']) / 'model.bin').read_bytes(), files['model.bin'])

    def test_whisper_process_termination_leaves_recoverable_unpublished_stage(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'preprocessor_config.json': b'{"fixture":"metadata"}',
            'model.bin': b'W' * (256 * 1024),
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, _, server, root, _ = self.make_hf_installer(
            files, role='asr', repo=repo, platform='linux-x86_64')
        server.drop_file = 'model.bin'
        code = (
            'import json, os, sys; '
            'from speechcraft_models.download import _Installer; '
            'try_install = lambda: _Installer(sys.argv[1], json.loads(sys.argv[2]), '
            'allow_test_http=True, retry_delay=0, test_hf_endpoint=sys.argv[3], '
            'attempts=1).install("asr", "linux-x86_64"); '
            '\ntry:\n try_install()\nexcept Exception:\n os._exit(47)'
        )
        process = subprocess.run(
            [sys.executable, '-c', code, str(root), json.dumps(installer.catalog),
             installer.test_hf_endpoint], timeout=15, check=False)
        self.assertEqual(process.returncode, 47)
        stage = next(root.glob('whisper-large-v3/.stage-*'))
        partials = list((stage / '.hf-source/.cache/huggingface/download').glob('*.incomplete'))
        self.assertTrue(partials, 'expected Hub interrupted-transfer state in staging')
        self.assertFalse(any(path.is_dir() and not path.name.startswith('.stage-')
                             for path in (root / 'whisper-large-v3').iterdir()))
        server.drop_file = None
        result = installer.install('asr', 'linux-x86_64')
        self.assertTrue(result['install_ready'])
        self.assertTrue(all(item['status'] == 'verified' for item in result['files']))
        for name, data in files.items():
            self.assertEqual((Path(result['root']) / name).read_bytes(), data)

    def test_huggingface_insufficient_disk_fails_before_acquisition(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        installer, _, _, server, _, _ = self.make_hf_installer({filename: b'model'})
        low = SimpleNamespace(f_bavail=1, f_frsize=1)
        with patch('speechcraft_models.download.os.fstatvfs', return_value=low):
            with self.assertRaisesRegex(InstallError, 'insufficient model-store disk space'):
                installer.install('speaker_purity', 'linux-x86_64', experimental=True)
        self.assertEqual(server.file_requests, [])

    def test_huggingface_checks_separate_xet_cache_capacity(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        installer, _, _, server, _, _ = self.make_hf_installer({filename: b'model'})
        from huggingface_hub import constants
        real_stat = os.stat
        cache_path = Path(constants.HF_XET_CACHE)
        def stat_on_other_volume(path, *args, **kwargs):
            if Path(path) == cache_path:
                return SimpleNamespace(st_dev=-1, st_mode=stat.S_IFDIR | 0o700)
            return real_stat(path, *args, **kwargs)
        root_fd = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch('speechcraft_models.download.os.fstatvfs',
                       return_value=SimpleNamespace(f_bavail=10**9, f_frsize=1)):
                with patch('speechcraft_models.download.os.stat', side_effect=stat_on_other_volume):
                    with patch('speechcraft_models.download.shutil.disk_usage',
                               return_value=SimpleNamespace(free=1)):
                        with self.assertRaisesRegex(InstallError, 'insufficient Hugging Face Xet-cache disk space'):
                            installer._check_huggingface_capacity(root_fd, 5)
        finally:
            os.close(root_fd)
        self.assertEqual(server.file_requests, [])

    def test_huggingface_capacity_accounts_only_configured_xet_cache_growth(self):
        filename = 'voxceleb_ECAPA512_LM.onnx'
        installer, _, _, server, _, events = self.make_hf_installer({filename: b'model'})
        from huggingface_hub import constants
        chunk_cache = Path(constants.HF_XET_CACHE) / 'fixture-region' / 'chunk_cache'
        chunk_cache.mkdir(parents=True)
        (chunk_cache / 'existing-chunk').write_bytes(b'x' * 4096)
        root_fd = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch.dict(os.environ, {'HF_XET_CHUNK_CACHE_SIZE_BYTES': '8192'}):
                installer._check_huggingface_capacity(root_fd, 10000)
        finally:
            os.close(root_fd)
        capacity = next(event for event in events if event['event'] == 'capacity')
        self.assertEqual(capacity['xet_chunk_cache_limit_bytes'], 8192)
        self.assertGreaterEqual(capacity['xet_chunk_cache_used_bytes'], 4096)
        expected_growth = max(0, min(8192, 10000) - capacity['xet_chunk_cache_used_bytes'])
        self.assertEqual(capacity['xet_chunk_cache_growth_required_bytes'], expected_growth)
        self.assertEqual(server.file_requests, [])

    def test_ecapa_artifact_selects_supported_platforms_with_verified_provenance(self):
        catalog = load_catalog()
        linux = select_artifact(catalog, 'speaker_purity', 'linux-x86_64')
        macos = select_artifact(catalog, 'speaker_purity', 'macos-arm64')
        self.assertEqual(linux['revision'], macos['revision'])
        self.assertEqual(linux['source']['repo_id'], 'Wespeaker/wespeaker-ecapa-tdnn512-LM')
        self.assertEqual(linux['required_files'][0]['path'], 'voxceleb_ECAPA512_LM.onnx')
        self.assertEqual(linux['required_files'][0]['size_bytes'], 24861931)
        self.assertEqual(linux['required_files'][0]['sha256'], 'd71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0')
        self.assertEqual(linux['required_files'], macos['required_files'])
        self.assertEqual(next(m for m in catalog['models'] if m['role'] == 'speaker_purity')['license']['status'], 'verified')
        self.assertTrue(_Installer(Path(self.temp.name) / 'verified', catalog).resolve('speaker_purity', 'macos-arm64'))

    def test_experimental_flag_cannot_bypass_model_release_blockers(self):
        catalog = load_catalog()
        model = next(item for item in catalog['models'] if item['role'] == 'speaker_purity')
        model['artifacts'][0]['release_blockers'] = ['fixture unresolved redistribution review']
        installer = _Installer(Path(self.temp.name) / 'blocked', catalog)
        with self.assertRaisesRegex(InstallError, 'release-blocked'):
            installer.resolve('speaker_purity', 'linux-x86_64', experimental=True)

    def test_whisper_platforms_select_distinct_trusted_artifacts_and_wsl_alias(self):
        catalog = load_catalog()
        linux = select_artifact(catalog, 'asr', 'linux-x86_64')
        wsl = select_artifact(catalog, 'asr', 'windows-wsl2')
        macos = select_artifact(catalog, 'asr', 'macos-arm64')
        self.assertEqual(linux['format'].split('/')[0], 'CTranslate2 ')
        self.assertEqual(linux, wsl)
        self.assertEqual(macos['source']['repo_id'], 'mlx-community/whisper-large-v3-mlx')
        self.assertNotEqual(macos['source']['repo_id'], linux['source']['repo_id'])
        with self.assertRaisesRegex(ManifestError, 'unsupported platform'):
            select_artifact(catalog, 'asr', 'windows-native')

    def test_whisper_huggingface_install_and_truthful_indeterminate_progress(self):
        repo = 'Systran/faster-whisper-large-v3'
        files = {
            'config.json': b'{"fixture":"config"}',
            'model.bin': b'fabricated CTranslate2 weights',
            'preprocessor_config.json': b'{"fixture":"preprocessor"}',
            'tokenizer.json': b'{"fixture":"tokenizer"}',
            'vocabulary.json': b'{"fixture":"vocabulary"}',
        }
        installer, _, artifact, server, _, events = self.make_hf_installer(
            files, role='asr', repo=repo, platform='windows-wsl2')
        result = installer.install('asr', 'windows-wsl2')
        self.assertTrue(result['install_ready'])
        self.assertEqual({item['path'] for item in result['files']}, set(files))
        self.assertEqual(server.file_requests, list(files))
        self.assertTrue(all(header is None for header in server.auth_headers))
        progress = [event for event in events if event['event'] == 'source_progress']
        self.assertTrue(progress)
        for event in progress:
            self.assertEqual(event['role'], 'asr')
            self.assertEqual(event['platform'], 'windows-wsl2')
            self.assertEqual(event['state'], 'downloading')
            self.assertIsNone(event['transferred_bytes'])
            self.assertIsNone(event['downloaded_bytes'])
            self.assertIsNone(event['current_file'])
            self.assertTrue(event['transfer_indeterminate'])
            self.assertEqual(event['total_bytes'], sum(map(len, files.values())))
        self.assertTrue(installer.install('asr', 'windows-wsl2')['reused'])
        self.assertTrue(artifact['inventory_complete'])
        self.assertTrue(any(e['event'] == 'installed' and e['state'] == 'complete'
                            for e in events))
        self.assertTrue(any(e['event'] == 'reused' and e['state'] == 'complete'
                            for e in events))

    def test_whisper_mlx_artifact_uses_shared_integrity_pipeline(self):
        repo = 'mlx-community/whisper-large-v3-mlx'
        files = {'config.json': b'{"model_type":"whisper"}', 'weights.npz': b'fake full precision MLX weights'}
        installer, _, _, server, _, events = self.make_hf_installer(
            files, role='asr', repo=repo, platform='macos-arm64')
        result = installer.install('asr', 'macos-arm64')
        self.assertTrue(result['integrity_verified'])
        self.assertTrue(result['install_ready'])
        self.assertIsNone(result['runtime_ready'])
        self.assertEqual(server.file_requests, ['config.json', 'weights.npz'])
        self.assertTrue(any(e['event'] == 'verifying' for e in events))
        self.assertTrue(any(e['event'] == 'publishing' for e in events))
        self.assertFalse(any(e['event'] == 'failed' for e in events))

    def test_wsl_model_store_rejects_ntfs_and_network_filesystems(self):
        for filesystem in ('ntfs', 'ntfs3', 'fuseblk', 'cifs', 'smbfs'):
            mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                         f'37 36 0:35 / /srv/windows rw,relatime - {filesystem} disk rw\n')
            with self.subTest(filesystem=filesystem):
                with patch('speechcraft_models.download.os.uname',
                           return_value=SimpleNamespace(release='6.6.1-microsoft-standard-WSL2')):
                    with patch('builtins.open', mock_open(read_data=mountinfo)):
                        with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                            _reject_wsl_windows_mount(Path('/srv/windows/models'), 'windows-wsl2')

    def test_wsl_model_store_rejects_windows_drive_mount(self):
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n')
        with patch('speechcraft_models.download.os.uname',
                   return_value=SimpleNamespace(release='6.6.1-microsoft-standard-WSL2')):
            with patch('builtins.open', mock_open(read_data=mountinfo)):
                with self.assertRaisesRegex(InstallError, 'WSL Linux filesystem'):
                    _reject_wsl_windows_mount(Path('/mnt/c/Users/test/models'), 'linux-x86_64')

    def test_wsl_storage_policy_is_host_detected_and_independent_of_selection(self):
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n'
                     '38 36 0:35 / /home/test/windows rw,relatime - 9p drvfs rw\n')
        with patch('speechcraft_models.download.os.uname',
                   return_value=SimpleNamespace(release='6.6.1-microsoft-standard-WSL2')):
            with patch('builtins.open', mock_open(read_data=mountinfo)):
                for platform in ('linux-x86_64', 'windows-wsl2'):
                    with self.subTest(platform=platform):
                        _reject_wsl_windows_mount(Path(self.temp.name) / 'models', platform)
                        for windows_mount in ('/mnt/c/Users/test/models', '/home/test/windows/models'):
                            with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                                _reject_wsl_windows_mount(Path(windows_mount), platform)
                        with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                            _Installer(Path('/mnt/c/SpeechCraft/models'), self.catalog).install(
                                'asr', platform)

    def test_wsl_storage_policy_follows_symlinks_and_fails_closed(self):
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n')
        alias = Path(self.temp.name) / 'windows-models'
        alias.symlink_to('/mnt/c/Users/test/models')
        with patch('speechcraft_models.download.os.uname',
                   return_value=SimpleNamespace(release='microsoft-standard-WSL2')):
            with patch('builtins.open', mock_open(read_data=mountinfo)):
                with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                    _reject_wsl_windows_mount(alias)
            with patch('builtins.open', side_effect=OSError('mountinfo unavailable')):
                with self.assertRaisesRegex(InstallError, 'cannot confirm WSL'):
                    _reject_wsl_windows_mount(Path(self.temp.name) / 'models')

    def test_wsl_environment_marker_is_runtime_evidence(self):
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n')
        with patch('speechcraft_models.download.os.uname',
                   return_value=SimpleNamespace(release='6.6.1-custom')):
            with patch('builtins.open', mock_open(read_data=mountinfo)):
                with patch.dict(os.environ, {'WSL_INTEROP': '/run/WSL/123_interop'}):
                    with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                        _reject_wsl_windows_mount(Path('/mnt/c/model-store'))

    def test_native_linux_and_macos_skip_wsl_restrictions(self):
        with patch('speechcraft_models.download._wsl_runtime', return_value=False):
            _reject_wsl_windows_mount(Path('/mnt/c/models'), 'linux-x86_64')
            _reject_wsl_windows_mount(Path('/mnt/c/models'), 'macos-arm64')

    def test_wsl_xet_cache_rejects_windows_drive_mount(self):
        from huggingface_hub import constants
        installer = _Installer(Path(self.temp.name) / 'models', self.catalog)
        token = installer._event_context.set({'role': 'asr', 'platform': 'windows-wsl2'})
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n')
        root_fd = os.open(self.temp.name, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with patch('speechcraft_models.download.os.uname',
                       return_value=SimpleNamespace(release='6.6.1-microsoft-standard-WSL2')):
                with patch('builtins.open', mock_open(read_data=mountinfo)):
                    with patch.object(constants, 'HF_XET_CACHE', '/mnt/c/Users/test/hf-xet'):
                        with self.assertRaisesRegex(InstallError, 'WSL Linux filesystem'):
                            installer._check_huggingface_capacity(root_fd, 10)
        finally:
            installer._event_context.reset(token)
            os.close(root_fd)

    def test_wsl_rejects_effective_hf_home_and_hub_cache_locations(self):
        from huggingface_hub import constants
        from speechcraft_models.download import _validate_huggingface_storage
        mountinfo = ('36 25 0:34 / / rw,relatime - ext4 /dev/sda rw\n'
                     '37 36 0:35 / /mnt/c rw,relatime - 9p drvfs rw\n')
        native_cache = str(Path(self.temp.name) / 'hf-cache')
        with patch('speechcraft_models.download.os.uname',
                   return_value=SimpleNamespace(release='microsoft-standard-WSL2')):
            with patch('builtins.open', mock_open(read_data=mountinfo)):
                with patch.object(constants, 'HF_HUB_CACHE', native_cache), \
                     patch.object(constants, 'HF_XET_CACHE', native_cache):
                    for field in ('HF_HOME', 'HF_HUB_CACHE', 'HF_XET_CACHE'):
                        with self.subTest(cache=field):
                            with patch.object(constants, field, '/mnt/c/Users/test/hf-cache'):
                                with self.assertRaisesRegex(InstallError, 'Windows-mounted'):
                                    _validate_huggingface_storage(Path(self.temp.name) / 'models')

    def test_invalid_file_paths_and_symlink_escapes(self):
        for path in ('../outside', '/outside', 'a/../../b', 'a\\b'):
            self.artifact['required_files'][0]['path'] = path
            with self.assertRaises(InstallError):
                self.install()
        self.artifact['required_files'][0]['path'] = 'nested/model.onnx'
        outside = Path(self.temp.name) / 'outside'
        outside.write_bytes(b'do not modify')
        stage = self.stage()
        stage.mkdir(parents=True)
        (stage / '0.part').symlink_to(outside)
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(outside.read_bytes(), b'do not modify')
        self.assertFalse(self.final().exists())

    def test_staging_directory_symlink_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        stage = self.stage()
        stage.parent.mkdir(parents=True)
        stage.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_fail_write_or_publish_never_claims_success(self):
        with patch('speechcraft_models.download._write', side_effect=OSError('write failed')):
            with self.assertRaises(InstallError):
                self.install()
        self.assertFalse(self.final().exists())
        with patch('speechcraft_models.download.os.rename', side_effect=OSError('publish failed')):
            with self.assertRaises(InstallError):
                self.install()
        self.assertFalse(self.final().exists())
        self.assertTrue(self.install()['install_ready'])

    def test_final_copy_or_publish_failure_preserves_previous_version(self):
        old = self.install()
        old_catalog = copy.deepcopy(self.catalog)
        self.artifact['revision'] = 'b' * 40
        with patch.object(self.installer, 'materialize', side_effect=OSError('final write failed')):
            with self.assertRaises(InstallError):
                self.install()
        with patch('speechcraft_models.download.os.rename', side_effect=OSError('publish denied')):
            with self.assertRaises(InstallError):
                self.install()
        self.assertFalse(self.final().exists())
        self.assertTrue(_Installer(self.root, old_catalog, allow_test_http=True).check('speech_detection', 'linux-x86_64')['install_ready'])
        self.assertEqual((Path(old['root']) / 'nested/model.onnx').read_bytes(), DATA)
        self.assertTrue(self.install()['install_ready'])

    def test_root_and_final_symlinks_are_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InstallError):
            self.install()
        self.root.unlink()
        final = self.final()
        final.parent.mkdir(parents=True)
        final.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(list(outside.iterdir()), [])

    def test_hardlinked_partial_is_not_modified(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_bytes(b'preserve')
        stage = self.stage()
        stage.mkdir(parents=True)
        os.link(outside, stage / '0.part')
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(outside.read_bytes(), b'preserve')

    def test_lock_wait_is_bounded(self):
        model_id, key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        with self.installer.store(model_id, True) as (_, model):
            with self.installer.lock(model, key):
                with self.assertRaisesRegex(InstallError, 'timed out'):
                    self.new_installer(lock_timeout=0).install('speech_detection', 'linux-x86_64')
        self.assertEqual(self.server.requests, [])

    def test_redirects_and_nonapproved_sources_are_rejected(self):
        self.server.modes = ['redirect']
        with self.assertRaises(InstallError):
            self.install()
        self.assertEqual(len(self.server.requests), 1)
        self.artifact['required_files'][0]['url'] = 'file:///etc/passwd'
        with self.assertRaises(InstallError):
            self.install()

    def test_untracked_crash_payload_is_not_published(self):
        stage = self.stage()
        (stage / 'payload-abandoned').mkdir(parents=True)
        (stage / 'payload-abandoned/unknown').write_bytes(b'stale')
        self.install()
        self.assertFalse((self.final() / 'unknown').exists())
        self.assertFalse(stage.exists())

    def test_large_transfer_hashes_linearly_and_checkpoints_by_interval(self):
        data = bytes(range(256)) * (64 * 1024)
        self.server.payloads['/model'] = data
        self.artifact['required_files'][0].update(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        class CountHash:
            def __init__(self):
                self.inner = hashlib.sha256()
            def update(self, chunk):
                counters['hashed'] += len(chunk)
                self.inner.update(chunk)
            def hexdigest(self):
                return self.inner.hexdigest()
            def copy(self):
                other = CountHash()
                other.inner = self.inner.copy()
                return other
        counters = {'hashed': 0, 'checkpoints': 0}
        from speechcraft_models import download
        real_checkpoint, real_fsync = download._checkpoint, os.fsync
        def checkpoint(*args, **kwargs):
            counters['checkpoints'] += 1
            return real_checkpoint(*args, **kwargs)
        with patch('speechcraft_models.download._sha256', CountHash), \
             patch('speechcraft_models.download._checkpoint', side_effect=checkpoint), \
             patch('speechcraft_models.download.os.fsync', wraps=real_fsync) as sync:
            result = self.install()
        chunks = math.ceil((len(data) + len(CONFIG)) / 65536)
        self.assertTrue(result['install_ready'])
        self.assertLessEqual(counters['hashed'], len(data) + len(CONFIG))
        self.assertLessEqual(counters['checkpoints'], math.ceil(len(data) / (4 * 1024 * 1024)) + 2)
        self.assertLess(sync.call_count, chunks // 8)

    def test_cleanup_preserves_recoverable_partials_and_expires_stale_stages(self):
        old_model, old_key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        recoverable = self.root / old_model / ('.stage-' + old_key)
        recoverable.mkdir(parents=True, mode=0o700)
        state_data = DATA[:1000]
        (recoverable / '0.part').write_bytes(state_data)
        (recoverable / '0.resume.json').write_text(json.dumps({
            'url': self.artifact['required_files'][0]['url'], 'etag': '"fixture-v1"',
            'size': 1000, 'sha256': hashlib.sha256(state_data).hexdigest()}))
        stale = self.root / old_model / ('.stage-' + 'a' * 64)
        stale.mkdir(mode=0o700)
        os.utime(stale, (time.time() - 8 * 86400, time.time() - 8 * 86400))
        # A second platform artifact triggers per-model cleanup while the old
        # artifact remains in the trusted catalog for partial-state validation.
        new_artifact = copy.deepcopy(self.artifact)
        new_artifact.update(platform='macos-arm64', revision='c' * 40)
        new_artifact.pop('supported_platforms')
        self.model['artifacts'].append(new_artifact)
        self.installer = self.new_installer()
        self.assertTrue(self.installer.install('speech_detection', 'macos-arm64')['install_ready'])
        self.assertTrue(recoverable.exists())
        self.assertFalse(stale.exists())
        self.assertTrue((recoverable / '0.part').exists())

    def test_cleanup_respects_active_other_artifact_lock(self):
        self.install()
        model_id, key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        other = 'd' * 64
        stage = self.root / model_id / ('.stage-' + other)
        stage.mkdir(mode=0o700)
        (stage / 'payload-old').mkdir()
        model_fd = os.open(self.root / model_id, os.O_RDONLY | os.O_DIRECTORY)
        lock_fd = os.open('.lock-' + other, os.O_CREAT | os.O_RDWR, 0o600, dir_fd=model_fd)
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.installer._cleanup_stages(model_fd, key)
            self.assertTrue((stage / 'payload-old').exists())
        finally:
            os.close(lock_fd)
            os.close(model_fd)
        model_fd = os.open(self.root / model_id, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.installer._cleanup_stages(model_fd, key)
        finally:
            os.close(model_fd)
        self.assertFalse((stage / 'payload-old').exists())

    def test_cleanup_unlinks_symlink_without_touching_target(self):
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        target = outside / 'keep'
        target.write_text('safe')
        model_id, key, _ = self.installer.resolve('speech_detection', 'linux-x86_64')
        (self.root / model_id).mkdir(parents=True, mode=0o700)
        stage = self.root / model_id / ('.stage-' + 'e' * 64)
        stage.mkdir(mode=0o700)
        (stage / 'payload-old').mkdir(mode=0o700)
        (stage / 'payload-old/escape').symlink_to(outside, target_is_directory=True)
        old = time.time() - 8 * 86400
        os.utime(stage, (old, old))
        model_fd = os.open(self.root / model_id, os.O_RDONLY | os.O_DIRECTORY)
        self.installer._cleanup_stages(model_fd, key)
        os.close(model_fd)
        self.assertEqual(target.read_text(), 'safe')
        self.assertFalse((stage / 'payload-old').exists())


if __name__ == '__main__':
    unittest.main()
