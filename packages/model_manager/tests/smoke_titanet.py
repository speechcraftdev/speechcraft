"""Opt-in 101 MB public Hub smoke; uses a disposable, isolated model store."""
import argparse
import json
import sys
import tempfile

from speechcraft_models import install_model


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download-real-titanet', action='store_true', required=True)
    parser.add_argument('--platform', required=True, choices=('linux-x86_64', 'macos-arm64'))
    args = parser.parse_args()
    def progress(event):
        print(json.dumps(event), file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory(prefix='speechcraft-titanet-smoke-') as root:
        first = install_model(root, 'speaker_purity', args.platform,
                              experimental=True, progress=progress)
        assert first['integrity_verified']
        assert not first['install_ready']  # Catalog release blockers remain active.
        assert first['runtime_ready'] is None
        second = install_model(root, 'speaker_purity', args.platform,
                               experimental=True, progress=progress)
        assert second['integrity_verified'] and second['reused']
        print(json.dumps({
            'result': 'file_integrity_verified',
            'platform': args.platform,
            'root': first['root'],
            'files': first['files'],
            'install_ready': first['install_ready'],
            'runtime_ready': first['runtime_ready'],
            'release_blockers': first['release_blockers'],
            'reused_on_repeat': second['reused'],
        }, indent=2))


if __name__ == '__main__':
    main()
