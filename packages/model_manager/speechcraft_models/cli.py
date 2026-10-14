"""Developer interface. Download sources come only from the packaged catalog."""
import argparse
import json
import sys

from .download import InstallError, check_installation, install_model
from .manifest import ManifestError, load_catalog, select_artifact, verify_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install or verify SpeechCraft model files (no inference).')
    parser.add_argument('action', choices=('install', 'status', 'verify'))
    parser.add_argument('role')
    parser.add_argument('--platform', required=True,
                        choices=('linux-x86_64', 'windows-wsl2', 'macos-arm64'))
    parser.add_argument('--root', required=True, help='Managed model store; for verify, the artifact directory itself')
    args = parser.parse_args(argv)
    try:
        if args.action == 'install':
            result = install_model(args.root, args.role, args.platform,
                                   progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
        elif args.action == 'status':
            result = check_installation(args.root, args.role, args.platform)
        else:
            result = verify_artifact(args.root, select_artifact(load_catalog(), args.role, args.platform))
        print(json.dumps(result, indent=2))
        return 0 if result['install_ready'] else 1
    except (InstallError, ManifestError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc), 'runtime_ready': None}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
