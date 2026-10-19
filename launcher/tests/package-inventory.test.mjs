import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { REQUIRED_RUNTIME_FILES, validateRuntimeInventory } from '../src/package-inventory.js';

function makeCompleteRuntimeRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-runtime-inventory-'));
  for (const relative of REQUIRED_RUNTIME_FILES) {
    const filename = path.join(root, relative);
    fs.mkdirSync(path.dirname(filename), { recursive: true });
    fs.writeFileSync(filename, relative === 'setup-controller/resources/runtime-manifest.json'
      ? JSON.stringify({ worker: { validation_helper: 'workers/dataset/scripts/preflight.py' } })
      : relative === 'frontend-release-manifest.json' ? JSON.stringify({ schema_version: 1, next_build_id: 'fixture', files: [] }) : 'fixture');
  }
  return root;
}

test('runtime inventory requires the model-manager local project used by the worker lock', () => {
  const root = makeCompleteRuntimeRoot();
  try {
    assert.equal(validateRuntimeInventory(root).ok, true);
    for (const relative of [
      'packages/model_manager/pyproject.toml',
      'packages/model_manager/README.md',
      'packages/model_manager/speechcraft_models/cli.py',
    ]) {
      fs.rmSync(path.join(root, relative));
      const result = validateRuntimeInventory(root);
      assert.equal(result.ok, false);
      assert.ok(result.missing.includes(relative), `${relative} must be reported missing`);
      fs.mkdirSync(path.dirname(path.join(root, relative)), { recursive: true });
      fs.writeFileSync(path.join(root, relative), 'fixture');
    }
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
