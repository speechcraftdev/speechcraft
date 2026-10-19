import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const page = fs.readFileSync(path.join(root, 'src/app/(setup)/setup/page.tsx'), 'utf8');
const catalog = JSON.parse(fs.readFileSync(path.join(root, '../packages/model_manager/speechcraft_models/model-catalog.json'), 'utf8'));

test('setup shows ECAPA publisher attribution and linked CC BY 4.0 notice', () => {
  const ecapa = catalog.models.find((model) => model.id === 'wespeaker-ecapa-tdnn512-lm');
  assert.ok(ecapa, 'the pinned ECAPA model remains in the approved catalog');
  assert.match(page, /model\.role === "speaker_purity"/);
  assert.match(page, /WeSpeaker ECAPA-TDNN512-LM, published by WeSpeaker/);
  assert.match(page, /a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b/);
  assert.match(page, /https:\/\/creativecommons\.org\/licenses\/by\/4\.0\//);
  assert.match(page, /https:\/\/huggingface\.co\/Wespeaker\/wespeaker-ecapa-tdnn512-LM/);
  assert.equal(ecapa.artifacts[0].revision, 'a2f3dcb1c8702caccc7a55ceb57f5e8d1842112b');
  assert.equal(ecapa.artifacts[0].required_files[0].sha256, 'd71b85d9b48058ef68004f04f1b78acebefb9dfcf542e19b976a12a5ad1f10b0');
});
