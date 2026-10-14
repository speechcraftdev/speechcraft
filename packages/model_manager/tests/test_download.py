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
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from speechcraft_models.download import _Installer, InstallError
from speechcraft_models.manifest import ManifestError, load_catalog

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

    def test_interruption_resumes_with_strong_validator(self):
        self.server.modes = ['drop', 'ok']
        with patch('speechcraft_models.download.CHECKPOINT_BYTES', 512):
            self.assertTrue(self.install()['install_ready'])
        self.assertEqual(self.server.requests[1][1:], ('bytes=1000-', '"fixture-v1"'))

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
