"""Opt-in network smoke; always uses a disposable model directory."""
import argparse
import json
import subprocess
import sys
import tempfile
from speechcraft_models import check_installation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download-real-silero', action='store_true', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='speechcraft-silero-smoke-') as root:
        command = [sys.executable, '-I', '-m', 'speechcraft_models', 'install', 'speech_detection',
                   '--platform', 'linux-x86_64', '--root', root]
        first_run = subprocess.run(command, capture_output=True, text=True, check=True)
        first = json.loads(first_run.stdout)
        assert first['install_ready'] and not first['reused']
        assert first['runtime_ready'] is None
        second = json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout)
        assert second['reused']
        assert check_installation(root, 'speech_detection', 'macos-arm64')['install_ready']
        print('PASS: installed CLI downloaded Silero and LICENSE, file-verified, and reused on repeat; inference not run')


if __name__ == '__main__':
    main()
