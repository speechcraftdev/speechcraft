"""Run from a copied directory with only the built wheel installed."""
import hashlib
from importlib import metadata, resources, util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from speechcraft_models import load_catalog, select_artifact, verify_artifact

requirements = metadata.requires('speechcraft-model-manager') or []
assert any(item.startswith('huggingface-hub==0.36.2') for item in requirements)
assert any(item.startswith('hf-xet==1.5.0') for item in requirements)
assert util.find_spec('torch') is None
assert util.find_spec('speechcraft_dataset') is None
assert not metadata.distribution('speechcraft-model-manager').entry_points
assert str(Path(sys.prefix).resolve()) in str(resources.files('speechcraft_models'))
json.loads(resources.files('speechcraft_models').joinpath('model-manifest.schema.json').read_text())
catalog = load_catalog()
artifact = select_artifact(catalog, 'speech_detection', 'linux-x86_64')
artifact['required_files'] = [{'path': 'tiny.bin', 'size_bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}]
with tempfile.TemporaryDirectory() as root:
    Path(root, 'tiny.bin').write_bytes(b'abc')
    assert verify_artifact(root, artifact)['install_ready']
    result = subprocess.run([sys.executable, '-I', '-m', 'speechcraft_models', 'status', 'speech_detection',
                             '--platform', 'linux-x86_64', '--root', root], capture_output=True, text=True)
    assert result.returncode == 1 and json.loads(result.stdout)['status'] == 'missing'
print('PASS: installed package, resources, offline verifier, and CLI outside checkout; no worker/ML dependencies')
