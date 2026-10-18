import assert from 'node:assert/strict';
import fs from 'node:fs';
import Module from 'node:module';
import path from 'node:path';
import { createRequire } from 'node:module';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(path.join(frontend, 'package.json'));
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const sourceFile = path.join(frontend, 'src/lib/setup-progress-view.tsx');
const compiled = ts.transpileModule(fs.readFileSync(sourceFile, 'utf8'), { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const loaded = new Module(sourceFile);
loaded.filename = sourceFile;
loaded.paths = Module._nodeModulePaths(path.dirname(sourceFile));
loaded._compile(compiled, sourceFile);
const { ProgressDetails } = loaded.exports;
const html = (progress) => renderToStaticMarkup(React.createElement(ProgressDetails,
  { progress, fallbackName: 'SpeechCraft setup' }));

test('byte-measured download renders size, percentage, speed, ETA, and an accessible progress bar', () => {
  const markup = html({ phase: 'downloading', model_name: 'Whisper Large-v3',
    downloaded_bytes: 1_420_000_000, total_bytes: 3_100_000_000,
    bytes_per_second: 28_400_000, eta_seconds: 59, transfer_indeterminate: false,
    files_completed: 1, files_total: 5 });
  assert.match(markup, /Whisper Large-v3/);
  assert.match(markup, /1\.42 GB \/ 3\.10 GB/);
  assert.match(markup, />45%<|45%/);
  assert.match(markup, /Speed: 28\.4 MB\/s/);
  assert.match(markup, /ETA: About 59 sec left/);
  assert.match(markup, /role="progressbar"[^>]*aria-valuenow="45"/);
});

test('Hugging Face file progress renders trusted total and counts without byte percentage, speed, or ETA', () => {
  const markup = html({ phase: 'downloading', model_name: 'pyannote Community-1',
    downloaded_bytes: null, total_bytes: 3_100_000_000, bytes_per_second: null,
    eta_seconds: null, files_completed: 3, files_total: 8, transfer_indeterminate: true });
  assert.match(markup, /Total model size: 3\.10 GB/);
  assert.match(markup, /3 \/ 8 files completed/);
  assert.match(markup, /Speed: unavailable/);
  assert.match(markup, /ETA: unavailable/);
  assert.match(markup, /role="progressbar"/);
  assert.doesNotMatch(markup, /aria-valuenow|\d+%/);
});

test('verification and inference phases do not render download percentage, speed, ETA, or a download bar', () => {
  for (const phase of ['verifying', 'installing', 'running_inference']) {
    const markup = html({ phase, model_name: 'Whisper Large-v3', downloaded_bytes: 1_000,
      total_bytes: 2_000, bytes_per_second: 100, eta_seconds: 10, transfer_indeterminate: false });
    assert.match(markup, new RegExp(phase === 'running_inference' ? 'Running inference'
      : phase === 'verifying' ? 'Verifying files' : 'Installing'));
    assert.match(markup, /Download progress is not shown during this phase/);
    assert.doesNotMatch(markup, /role="progressbar"|Speed:|ETA:|aria-valuenow|\d+%/);
  }
});
