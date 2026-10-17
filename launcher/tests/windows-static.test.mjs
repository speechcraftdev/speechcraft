import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const windowsRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../windows');

function sourceText() {
  const files = [];
  const visit = (directory) => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const filename = path.join(directory, entry.name);
      if (entry.isDirectory()) visit(filename);
      else if (entry.name.endsWith('.go') && !entry.name.endsWith('_test.go')) files.push(fs.readFileSync(filename, 'utf8'));
    }
  };
  visit(windowsRoot);
  files.push(fs.readFileSync(path.resolve(windowsRoot, '../installer/install-windows.ps1'), 'utf8'));
  return files.join('\n');
}

test('Windows integration sources do not interpolate shell commands or unregister WSL', () => {
  const text = sourceText();
  for (const forbidden of ['bash -c', 'cmd.exe /c', '--unregister', 'portproxy', '0.0.0.0']) {
    assert.equal(text.includes(forbidden), false, forbidden);
  }
});
