#!/usr/bin/env bash
set -euo pipefail
package_root="$(cd "$(dirname "$0")/.." && pwd)"
task_tmp="$(mktemp -d /tmp/speechcraft-package-test.XXXXXX)"
trap 'rm -rf "$task_tmp"' EXIT
uv build --wheel --out-dir "$task_tmp/dist" "$package_root"
uv venv --python 3.12 "$task_tmp/env"
uv pip install --python "$task_tmp/env/bin/python" "huggingface-hub==0.36.2" "hf-xet==1.5.0"
uv pip install --python "$task_tmp/env/bin/python" --no-deps --no-index "$task_tmp"/dist/*.whl
cp -R "$package_root/tests" "$task_tmp/tests"
cd "$task_tmp"
unset PYTHONPATH
"$task_tmp/env/bin/python" -I tests/installed_smoke.py
"$task_tmp/env/bin/python" -m unittest discover -s tests -p 'test_*.py' -v
