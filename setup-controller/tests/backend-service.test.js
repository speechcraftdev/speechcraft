import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { BackendService, reconcileOwnedService } from '../src/backend-service.js';
import { StateStore } from '../src/state-store.js';

test('backend service is a managed process group, requires health, and stops cleanly', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-managed-backend-'));
  const data = path.join(root, 'data');
  const runtimes = path.join(data, 'runtimes', 'fixture');
  const bin = path.join(runtimes, 'bin');
  const appRoot = path.join(root, 'release');
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.join(appRoot, 'backend'), { recursive: true });
  const executable = path.join(bin, 'python');
  fs.writeFileSync(executable, `#!/usr/bin/env node\nconst http=require('node:http'); const a=process.argv; const c=a[a.indexOf('-c')+1]||''; const m=c.match(/port=(\\d+)/); if (!m) process.exit(3); http.createServer((q,r)=>{r.setHeader('content-type','application/json');r.end(JSON.stringify({status:'ok'}));}).listen(Number(m[1]),'127.0.0.1');\n`);
  fs.chmodSync(executable, 0o755);
  const store = StateStore.open(path.join(root, 'state'));
  const serviceStatuses = [];
  const service = new BackendService({ store, dataDirectory: data, applicationRoot: appRoot,
    environment: { profile: 'linux-x86_64' }, pythonPath: executable, environmentVars: { PATH: process.env.PATH },
    attemptId: 'c44d552a-8e3d-4af9-98d6-e603ce9dd2e1', onStatus: (status) => serviceStatuses.push(status) });
  try {
    const started = await service.start(new AbortController().signal);
    assert.ok(started.port > 0);
    const record = store.snapshot().service_process;
    assert.equal(record.state, 'running');
    assert.equal((await service.verify()).health, 'ok');
    const firstPid = record.pid;
    process.kill(firstPid, 'SIGKILL');
    const deadline = Date.now() + 5_000;
    while (Date.now() < deadline) {
      if (serviceStatuses.some((status) => status.message === 'Backend recovered after one bounded restart.')) break;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.notEqual(store.snapshot().service_process?.pid, firstPid, 'one bounded restart should replace an exited backend');
    assert.ok(serviceStatuses.some((status) => status.message === 'Backend recovered after one bounded restart.'));
    assert.equal((await service.verify()).health, 'ok');
    await service.stop();
    assert.equal(store.snapshot().service_process, null);
  } finally {
    await service.stop().catch(() => {});
    store.releaseOwnership();
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('recovery stops an owned backend after controller restart', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-backend-recovery-'));
  const data = path.join(root, 'data');
  const bin = path.join(data, 'runtimes', 'fixture', 'bin');
  const appRoot = path.join(root, 'release');
  fs.mkdirSync(bin, { recursive: true }); fs.mkdirSync(path.join(appRoot, 'backend'), { recursive: true });
  const executable = path.join(bin, 'python');
  fs.writeFileSync(executable, `#!/usr/bin/env node\nconst http=require('node:http'); const a=process.argv; const c=a[a.indexOf('-c')+1]||''; const m=c.match(/port=(\\d+)/); http.createServer((q,r)=>r.end('{"status":"ok"}')).listen(Number(m[1]),'127.0.0.1');\n`);
  fs.chmodSync(executable, 0o755);
  const store = StateStore.open(path.join(root, 'state'));
  const service = new BackendService({ store, dataDirectory: data, applicationRoot: appRoot,
    environment: { profile: 'linux-x86_64' }, pythonPath: executable, environmentVars: { PATH: process.env.PATH },
    attemptId: 'c44d552a-8e3d-4af9-98d6-e603ce9dd2e1' });
  try {
    await service.start(new AbortController().signal);
    const ownedPid = store.snapshot().service_process.pid;
    assert.equal((await reconcileOwnedService(store)).status, 'termination_confirmed');
    assert.equal(store.snapshot().service_process, null);
    assert.throws(() => process.kill(ownedPid, 0), { code: 'ESRCH' },
      'reconciliation must stop the actual service process, not only clear its record');
  } finally { store.releaseOwnership(); fs.rmSync(root, { recursive: true, force: true }); }
});
