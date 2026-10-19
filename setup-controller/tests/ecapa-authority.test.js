import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { runtimeManifest, initialComponents } from '../src/runtime-plan.js';

const repositoryRoot = path.resolve(import.meta.dirname, '../..');
const readJson = (relative) => JSON.parse(fs.readFileSync(path.join(repositoryRoot, relative), 'utf8'));

test('ECAPA is the sole active speaker purity model and TitaNet is absent from setup and processing', () => {
  const catalog = readJson('packages/model_manager/speechcraft_models/model-catalog.json');
  const manifest = runtimeManifest;
  const speakerModel = catalog.models.filter((model) => model.role === 'speaker_purity');
  assert.equal(speakerModel.length, 1);
  assert.equal(speakerModel[0].id, 'wespeaker-ecapa-tdnn512-lm');
  assert.equal(speakerModel[0].name, 'WeSpeaker ECAPA-TDNN512-LM ONNX speaker embedding model');
  assert.equal(speakerModel[0].license.identifier, 'CC-BY-4.0');

  const artifact = speakerModel[0].artifacts.find((item) => item.platform === 'all-supported');
  assert.equal(artifact.revision, 'a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b');
  assert.deepEqual(artifact.required_files, [{
    path: 'voxceleb_ECAPA512_LM.onnx',
    size_bytes: 24861931,
    sha256: 'd71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0',
  }]);
  assert.ok(manifest.models.approved_roles.includes('speaker_purity'));
  assert.ok(Object.hasOwn(initialComponents(), 'ecapa'));
  assert.ok(!Object.hasOwn(initialComponents(), 'titanet'));

  const activePaths = [
    'setup-controller/src/runtime-plan.js',
    'setup-controller/src/runtime-tasks.js',
    'packages/model_manager/speechcraft_models/model-catalog.json',
    'workers/dataset/speechcraft_dataset/run.py',
    'workers/dataset/speechcraft_dataset/eval_speaker_purity.py',
    'workers/dataset/speechcraft_dataset/wespeaker_ecapa_runtime.py',
    'workers/dataset/scripts/runtime_probe.py',
  ];
  for (const relative of activePaths) {
    assert.doesNotMatch(fs.readFileSync(path.join(repositoryRoot, relative), 'utf8'), /titanet/i, relative);
  }
  assert.ok(fs.existsSync(path.join(repositoryRoot,
    'workers/dataset/speechcraft_dataset/wespeaker_ecapa_runtime.py')));
  assert.ok(!fs.existsSync(path.join(repositoryRoot,
    'workers/dataset/speechcraft_dataset/titanet_runtime.py')));
});
