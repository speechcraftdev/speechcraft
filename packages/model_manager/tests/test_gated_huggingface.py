"""Offline gated-source tests using tiny files and the real installer transaction."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from speechcraft_models.download import _Installer
from speechcraft_models.cli import _safe_diagnostic_from_chain
from speechcraft_models.huggingface_source import (
    GATED_REPOSITORIES, HuggingFaceAcquisitionError, _safe_exception_diagnostic, acquire_gated_snapshot,
    validate_gated_request,
)
from speechcraft_models.manifest import load_catalog
from speechcraft_models.manifest import select_artifact


TOKEN = 'hf_TEST_SECRET_MARKER_' + 't' * 24
REPO = 'speechcraft-test/community1-fixture'
REVISION = 'a' * 40
FILES = {
    'config.yaml': b'pipeline: tiny-test\n',
    'embedding/pytorch_model.bin': b'fixture-embedding' * 7,
    'plda/plda.npz': b'fixture-plda',
}


class FakeHubError(Exception):
    def __init__(self, status):
        super().__init__('fake upstream diagnostic containing no credential')
        self.response = SimpleNamespace(status_code=status)


class FakeApi:
    def __init__(self, *, access=True, identity=True, files=None):
        self.access = access
        self.identity = identity
        self.files = set(FILES) if files is None else set(files)
        self.calls = []

    def whoami(self, *, token):
        self.calls.append(('whoami', token))
        if not self.identity:
            raise FakeHubError(401)
        return {'name': 'test-user'}

    def model_info(self, repo_id, *, revision, token, files_metadata):
        self.calls.append(('model_info', repo_id, revision, token, files_metadata))
        if not self.access:
            raise FakeHubError(403)
        return SimpleNamespace(sha=REVISION,
            siblings=[SimpleNamespace(rfilename=name) for name in self.files])


def tiny_catalog():
    catalog = copy.deepcopy(load_catalog())
    model = next(item for item in catalog['models'] if item['role'] == 'diarization')
    model['id'] = 'community1-test'
    model['license'] = {'identifier': 'CC0-1.0', 'status': 'verified', 'source': 'offline test fixture'}
    artifact = model['artifacts'][0]
    artifact.update(revision=REVISION, availability='verified', inventory_complete=True, strict_tree=True,
        source={'type': 'huggingface', 'repo_id': REPO, 'authentication': 'gated'},
        release_blockers=[])
    artifact['required_files'] = [
        {'path': name, 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        for name, data in FILES.items()
    ]
    artifact['provenance'] = 'Offline synthetic test-only artifact.'
    return catalog


class GatedAcquisitionTests(unittest.TestCase):
    def test_huggingface_transfer_diagnostic_contains_only_exception_type_and_status(self):
        class SignedUrlFailure(Exception):
            response = SimpleNamespace(status_code=503)

        error = SignedUrlFailure('hf_secret https://storage.example/file?X-Amz-Signature=secret')
        self.assertEqual(_safe_exception_diagnostic(error), {
            'exception_type': 'SignedUrlFailure', 'http_status': 503,
        })
        self.assertNotIn('hf_secret', json.dumps(_safe_exception_diagnostic(error)))
        self.assertNotIn('X-Amz-Signature', json.dumps(_safe_exception_diagnostic(error)))

        acquisition = HuggingFaceAcquisitionError('safe message', diagnostic={
            'exception_type': 'SignedUrlFailure', 'http_status': 503,
            'url': 'https://storage.example/file?token=secret',
        })
        install_error = RuntimeError('nested wrapper')
        install_error.__cause__ = acquisition
        self.assertEqual(_safe_diagnostic_from_chain(install_error), {
            'exception_type': 'SignedUrlFailure', 'http_status': 503,
        })

        try:
            missing = AttributeError('private upstream text', 'ignored', 'missing_member')
            missing.name = 'missing_member'
            missing.obj = object()
            raise missing
        except AttributeError as caught:
            detail = _safe_exception_diagnostic(caught)
        self.assertEqual(detail['exception_type'], 'AttributeError')
        self.assertEqual(detail['attribute'], 'missing_member')
        self.assertEqual(detail['object_type'], 'object')
        self.assertTrue(detail['frames'])
        self.assertNotIn('private upstream text', json.dumps(detail))

    def test_production_community_catalog_matches_the_independent_gate_allowlist(self):
        catalog = load_catalog()
        artifact = select_artifact(catalog, 'diarization', 'linux-x86_64')
        model = next(item for item in catalog['models'] if item['role'] == 'diarization')
        source = artifact['source']
        self.assertEqual(source['authentication'], 'gated')
        self.assertEqual(source['repo_id'], 'pyannote/speaker-diarization-community-1')
        self.assertEqual(artifact['revision'], GATED_REPOSITORIES[source['repo_id']]['revision'])
        self.assertEqual({item['path'] for item in artifact['required_files']},
                         set(GATED_REPOSITORIES[source['repo_id']]['files']))
        self.assertTrue(artifact['strict_tree'])
        self.assertTrue(artifact['inventory_complete'])
        self.assertEqual(model['license']['identifier'], 'CC-BY-4.0')
        self.assertEqual(model['license']['status'], 'verified')

    def setup_fake(self, *, access=True, identity=True, interrupt_once=False):
        api = FakeApi(access=access, identity=identity)
        attempts = {'snapshot': 0}

        def download_file(*, repo_id, filename, revision, token, local_dir, force_download):
            assert (repo_id, filename, revision, token) == (REPO, 'config.yaml', REVISION, TOKEN)
            target = Path(local_dir) / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(FILES[filename])
            return str(target)

        def download_snapshot(*, repo_id, repo_type, revision, token, local_dir, allow_patterns, max_workers, tqdm_class):
            assert (repo_id, repo_type, revision, token) == (REPO, 'model', REVISION, TOKEN)
            assert set(allow_patterns) == set(FILES)
            # huggingface_hub uses tqdm.contrib.concurrent.thread_map even
            # with one worker; its lock helpers must be inherited by the hook.
            assert callable(getattr(tqdm_class, 'get_lock', None))
            attempts['snapshot'] += 1
            root = Path(local_dir)
            if interrupt_once and attempts['snapshot'] == 1:
                partial = root / 'embedding' / 'pytorch_model.bin'
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(FILES['embedding/pytorch_model.bin'][:5])
                raise FakeHubError(503)
            bar = tqdm_class(total=len(FILES))
            for name, data in FILES.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                bar.update(1)
            # Exercise the official local_dir metadata lifecycle.
            metadata = root / '.cache' / 'huggingface'
            metadata.mkdir(parents=True, exist_ok=True)
            (metadata / 'metadata.json').write_text('{"resume":true}')
            return str(root)

        return api, (api, download_file, download_snapshot), attempts

    def test_gated_install_uses_identity_access_and_exact_inventory_then_reuses(self):
        catalog = tiny_catalog()
        api, client, attempts = self.setup_fake()
        progress = []
        with tempfile.TemporaryDirectory() as tmp, patch.dict(GATED_REPOSITORIES, {
            REPO: {'revision': REVISION, 'files': frozenset(FILES), 'access_file': 'config.yaml'}
        }):
            result = _Installer(Path(tmp) / 'models', catalog, progress=progress.append,
                gated_token=TOKEN, gated_hub_client=client, retry_delay=0).install('diarization', 'linux-x86_64')
            self.assertTrue(result['install_ready'])
            self.assertTrue(result['inventory_exact'])
            file_progress = [event['completed_files'] for event in progress if event.get('progress_mode') == 'files']
            self.assertEqual(file_progress, list(range(1, len(FILES) + 1)))
            self.assertEqual([call[0] for call in api.calls], ['whoami', 'model_info'])
            self.assertEqual(attempts['snapshot'], 1)
            self.assertFalse((Path(result['root']) / '.cache').exists())
            status = _Installer(Path(tmp) / 'models', catalog).check('diarization', 'linux-x86_64')
            self.assertTrue(status['install_ready'])
            self.assertFalse(any(TOKEN in json.dumps(event) for event in progress))

    def test_invalid_token_and_gated_denial_stop_before_snapshot(self):
        with patch.dict(GATED_REPOSITORIES, {
            REPO: {'revision': REVISION, 'files': frozenset(FILES), 'access_file': 'config.yaml'}
        }):
            with self.assertRaisesRegex(HuggingFaceAcquisitionError, 'HF_TOKEN_INVALID'):
                validate_gated_request(REPO, REVISION, list(FILES), 'not-a-token')
        api, client, attempts = self.setup_fake(access=False)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(GATED_REPOSITORIES, {
            REPO: {'revision': REVISION, 'files': frozenset(FILES), 'access_file': 'config.yaml'}
        }):
            with self.assertRaisesRegex(HuggingFaceAcquisitionError, 'HF_MODEL_ACCESS_REQUIRED'):
                acquire_gated_snapshot(REPO, REVISION, list(FILES), TOKEN, Path(tmp) / 'snapshot', lambda _: None,
                    total_bytes=sum(map(len, FILES.values())), hub_client=client)
        self.assertEqual(api.calls[-1][0], 'model_info')
        self.assertEqual(attempts['snapshot'], 0)

    def test_missing_required_file_fails_access_check_and_rate_limits_retry_boundedly(self):
        catalog = tiny_catalog()
        with patch.dict(GATED_REPOSITORIES, {
            REPO: {'revision': REVISION, 'files': frozenset(FILES), 'access_file': 'config.yaml'}
        }):
            missing_api = FakeApi(files={'config.yaml'})
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(HuggingFaceAcquisitionError, 'HF_SNAPSHOT_INCOMPLETE'):
                    acquire_gated_snapshot(REPO, REVISION, list(FILES), TOKEN, Path(tmp), lambda _event: None,
                        total_bytes=sum(map(len, FILES.values())), hub_client=(missing_api,
                            lambda **_: self.fail('no file read should occur'),
                            lambda **_: self.fail('no snapshot should start')))
            self.assertEqual([call[0] for call in missing_api.calls], ['whoami', 'model_info'])

            class RateLimitedApi(FakeApi):
                def whoami(self, *, token):
                    self.calls.append(('whoami', token))
                    raise FakeHubError(429)

            rate_api = RateLimitedApi()
            with tempfile.TemporaryDirectory() as tmp:
                installer = _Installer(Path(tmp) / 'models', catalog, gated_token=TOKEN,
                    gated_hub_client=(rate_api, None, None), retry_delay=0)
                with self.assertRaisesRegex(Exception, 'HF_RATE_LIMITED'):
                    installer.install('diarization', 'linux-x86_64')
            self.assertEqual(len(rate_api.calls), installer.attempts)

    def test_interrupted_transfer_retries_and_same_size_tampering_is_not_ready(self):
        catalog = tiny_catalog()
        api, client, attempts = self.setup_fake(interrupt_once=True)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(GATED_REPOSITORIES, {
            REPO: {'revision': REVISION, 'files': frozenset(FILES), 'access_file': 'config.yaml'}
        }):
            root = Path(tmp) / 'models'
            result = _Installer(root, catalog, gated_token=TOKEN, gated_hub_client=client,
                retry_delay=0).install('diarization', 'linux-x86_64')
            self.assertTrue(result['install_ready'])
            self.assertEqual(attempts['snapshot'], 2)
            weight = Path(result['root']) / 'embedding' / 'pytorch_model.bin'
            original = weight.read_bytes()
            weight.write_bytes(b'X' * len(original))
            checked = _Installer(root, catalog).check('diarization', 'linux-x86_64')
            self.assertFalse(checked['install_ready'])
            self.assertIn('embedding/pytorch_model.bin', checked['corrupt_files'])


if __name__ == '__main__':
    unittest.main()
