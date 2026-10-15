"""Developer interface. Download sources come only from the packaged catalog."""
import argparse
import json
import sys

import re

from .download import InstallError, check_installation, install_gated_model, install_model
from .manifest import ManifestError, load_catalog, select_artifact, verify_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description='Install or verify SpeechCraft model files (no inference).')
    parser.add_argument('action', choices=('install', 'status', 'verify', 'authorize-install'))
    parser.add_argument('role')
    parser.add_argument('--platform', required=True,
                        choices=('linux-x86_64', 'windows-wsl2', 'macos-arm64'))
    parser.add_argument('--root', required=True, help='Managed model store; for verify, the artifact directory itself')
    parser.add_argument('--token-stdin', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == 'authorize-install':
            if args.role != 'diarization' or not args.token_stdin:
                raise InstallError('gated install requires the diarization role and private stdin credential input')
            credential = bytearray(sys.stdin.buffer.read(517))
            if len(credential) > 516 or not credential.endswith(b'\n'):
                credential[:] = b'\0' * len(credential)
                raise InstallError('HF_TOKEN_INVALID: credential input is empty, oversized, or malformed')
            raw_token = bytes(credential[:-1])
            credential[:] = b'\0' * len(credential)
            try:
                token = raw_token.decode('ascii')
            except UnicodeDecodeError:
                raise InstallError('HF_TOKEN_INVALID: credential format is invalid') from None
            del raw_token
            if re.fullmatch(r'hf_[A-Za-z0-9_-]{16,512}', token) is None:
                token = ''
                raise InstallError('HF_TOKEN_INVALID: credential format is invalid')
            try:
                result = install_gated_model(args.root, args.role, args.platform, token,
                    progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
            finally:
                token = ''
        elif args.action == 'install':
            result = install_model(args.root, args.role, args.platform,
                                   progress=lambda event: print(json.dumps(event), file=sys.stderr, flush=True))
        elif args.action == 'status':
            result = check_installation(args.root, args.role, args.platform)
        else:
            result = verify_artifact(args.root, select_artifact(load_catalog(), args.role, args.platform))
        print(json.dumps(result, indent=2))
        return 0 if result['install_ready'] else 1
    except (InstallError, ManifestError, OSError) as exc:
        message = str(exc)
        code_match = re.match(r'([A-Z][A-Z0-9_]{2,79}): ', message)
        code = code_match.group(1) if code_match else 'MODEL_INSTALL_FAILED'
        safe = message[code_match.end():] if code_match else message
        if args.action == 'authorize-install':
            allowed = {'HF_TOKEN_INVALID', 'HF_MODEL_ACCESS_REQUIRED', 'HF_TOKEN_PERMISSION_DENIED',
                       'HF_APPROVAL_PENDING', 'HF_NETWORK_ERROR', 'HF_RATE_LIMITED',
                       'HF_SNAPSHOT_INCOMPLETE', 'HF_SNAPSHOT_INTEGRITY_FAILED',
                       'MODEL_STORAGE_INSUFFICIENT', 'AUTHORIZATION_REQUIRED_AFTER_RESTART'}
            if code not in allowed:
                code, safe = 'MODEL_INSTALL_FAILED', 'Community-1 authorization or installation failed safely.'
        print(json.dumps({'status': 'error', 'error_code': code, 'error': safe[:320], 'runtime_ready': None}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
