"""Opt-in real Whisper Large-v3 acquisition test; never run by the unit suite."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time


def _directory_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            try:
                if path.is_file() and not path.is_symlink():
                    info = path.stat()
                    total += getattr(info, 'st_blocks', 0) * 512 or info.st_size
            except OSError:
                pass
    return total


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--download-real-whisper', action='store_true', required=True)
    parser.add_argument('--platform', required=True,
                        choices=('linux-x86_64', 'windows-wsl2', 'macos-arm64'))
    parser.add_argument('--model-root', type=Path,
                        help='Isolated persistent test model store; enables interruption/retry runs')
    parser.add_argument('--cache-root', type=Path,
                        help='Isolated persistent HF/Xet cache; otherwise created beside temporary model store')
    args = parser.parse_args()

    temporary_root = None
    interrupted = False
    if args.model_root is None:
        temporary_root = Path(tempfile.mkdtemp(prefix='speechcraft-whisper-smoke-'))
        model_root = temporary_root / 'models'
    else:
        model_root = args.model_root.expanduser().absolute()
        model_root.mkdir(parents=True, exist_ok=True)
    cache_root = (args.cache_root.expanduser().absolute() if args.cache_root
                  else (temporary_root / 'hf-xet-cache' if temporary_root
                        else model_root.parent / (model_root.name + '-hf-xet-cache')))
    cache_root.mkdir(parents=True, exist_ok=True)
    hf_home = cache_root.parent / (cache_root.name + '-home')
    hf_home.mkdir(parents=True, exist_ok=True)
    os.environ['HF_XET_CACHE'] = str(cache_root)
    os.environ['HF_HOME'] = str(hf_home)
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'

    # Import only after selecting isolated cache locations; Hub constants read
    # their environment configuration when imported.
    from speechcraft_models import install_model
    from speechcraft_models.manifest import load_catalog, select_artifact
    artifact = select_artifact(load_catalog(), 'asr', args.platform)
    revision = artifact['revision']
    expected_bytes = sum(item['size_bytes'] for item in artifact['required_files'])

    def progress(event):
        print(json.dumps(event), file=sys.stderr, flush=True)

    try:
        before_store = _directory_bytes(model_root)
        before_cache = _directory_bytes(cache_root)
        start = time.monotonic()
        first = install_model(model_root, 'asr', args.platform, progress=progress)
        elapsed = time.monotonic() - start
        second_start = time.monotonic()
        second = install_model(model_root, 'asr', args.platform, progress=progress)
        reuse_elapsed = time.monotonic() - second_start
        if not first['integrity_verified'] or not first['install_ready']:
            raise RuntimeError('downloaded Whisper artifact is not file-verified/install-ready')
        if not second['reused']:
            raise RuntimeError('repeat installation did not reuse the verified model')
        print(json.dumps({
            'result': 'file_integrity_verified',
            'role': 'asr', 'platform': args.platform,
            'revision': revision, 'root': first['root'],
            'expected_artifact_bytes': expected_bytes,
            'verified_model_payload_bytes': sum(item.get('size_bytes', 0) for item in first['files']),
            'transferred_bytes_observed': None,
            'transfer_byte_note': 'The pinned Hub progress API reports file completion, not trustworthy transferred bytes.',
            'files': first['files'],
            'model_store_bytes_before': before_store,
            'model_store_bytes_after': _directory_bytes(model_root),
            'xet_cache_bytes_before': before_cache,
            'xet_cache_bytes_after': _directory_bytes(cache_root),
            'download_and_install_seconds': elapsed,
            'verified_reuse_seconds': reuse_elapsed,
            'reused_on_repeat': second['reused'],
            'install_ready': first['install_ready'],
            'runtime_ready': first['runtime_ready'],
            'temporary_paths_removed_at_exit': temporary_root is not None,
        }, indent=2))
        return 0
    except KeyboardInterrupt:
        interrupted = True
        resume = [sys.executable, '-I', str(Path(__file__).resolve()),
                  '--download-real-whisper', '--platform', args.platform,
                  '--model-root', str(model_root), '--cache-root', str(cache_root)]
        print(json.dumps({
            'result': 'interrupted_recoverable_attempt',
            'model_root': str(model_root), 'cache_root': str(cache_root),
            'resume_command': shlex.join(resume),
        }, indent=2), file=sys.stderr)
        return 130
    except Exception as exc:
        print(json.dumps({
            'result': 'failed', 'role': 'asr', 'platform': args.platform,
            'error': str(exc), 'model_root': str(model_root),
            'cache_root': str(cache_root),
        }, indent=2), file=sys.stderr)
        return 1
    finally:
        if temporary_root is not None and not interrupted:
            shutil.rmtree(temporary_root, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
