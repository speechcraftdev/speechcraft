import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { BootstrapTasks } from '../src/bootstrap.js';
import { createSetupController } from '../src/server.js';
import { readUntilEvent, openEvents } from './helpers.js';

test('model-manager JSON telemetry flows through bootstrap, status, model rows, SSE, and rendering contract', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-progress-flow-'));
  const tasks = new BootstrapTasks({ dataDirectory: directory });
  tasks.stages = ['install_models'];
  tasks.environment = { profile: 'linux-x86_64', wsl: false };
  const cache = path.join(directory, 'cache', 'bootstrap');
  tasks.paths = { environmentRoot: path.join(directory, 'environments', 'model-manager'),
    hfHome: path.join(cache, 'huggingface'), hfHub: path.join(cache, 'huggingface', 'hub'),
    hfXet: path.join(cache, 'huggingface', 'xet'), temporary: path.join(cache, 'tmp') };
  tasks.makeEnvironment = () => ({});
  const reports = new Map();
  tasks.runProcess = async (_executable, args, _context, options = {}) => {
    const command = args[args.indexOf('install') + 1];
    if (command === 'speech_detection' || command === 'asr') {
      const total = command === 'asr' ? 2_000_000 : 1_000;
      const role = command;
        const artifactName = command === 'asr' ? 'Systran/faster-whisper-large-v3' : 'silero_vad.onnx';
      const events = [
        { event: 'downloading', role, artifact_name: artifactName, artifact_type: 'model',
          path: command === 'asr' ? 'weights/model.bin' : 'silero/model.onnx',
          attempt: 1, downloaded_bytes: 0, total_bytes: total, transfer_attempt_bytes: 0,
          files_completed: 0, files_total: 1 },
        { event: 'progress', role, artifact_name: artifactName, artifact_type: 'model',
          path: command === 'asr' ? 'weights/model.bin' : 'silero/model.onnx',
          attempt: 1, downloaded_bytes: Math.floor(total / 2), total_bytes: total,
          transfer_attempt_bytes: Math.floor(total / 2), files_completed: 0, files_total: 1 },
      ];
      for (const event of events) {
        options.onOutput?.('stderr', Buffer.from(`${JSON.stringify(event)}\n`));
        await new Promise((resolve) => setTimeout(resolve, 800));
      }
      reports.set(command, { status: 'verified', install_ready: true, root: path.join(directory, 'models', command) });
      return { code: 0, stdout: JSON.stringify(reports.get(command)), stderr: '' };
    }
    assert.ok(args.includes('status'));
    return { code: 0, stdout: JSON.stringify({ status: 'verified', install_ready: true,
      integrity_verified: true, root: path.join(directory, 'models', 'diarization') }), stderr: '' };
  };

  const app = await createSetupController({ dataDirectory: directory, port: 0, tasks, mode: 'bootstrap' });
  const base = `http://127.0.0.1:${app.port}`;
  let stream;
  try {
    const bootstrap = fs.readFileSync(app.bootstrapPath, 'utf8').trim();
    const session = await fetch(`${base}/setup-api/session`, { method: 'POST',
      headers: { origin: base, authorization: `Bearer ${bootstrap}`, 'content-type': 'application/json' }, body: '{}' });
    assert.equal(session.status, 201);
    const cookie = session.headers.get('set-cookie').split(';', 1)[0];
    const auth = await session.json();
    const apiRequest = (route, options = {}) => fetch(`${base}${route}`, { ...options, headers: {
      origin: base, cookie, ...(options.method && options.method !== 'GET' ? { 'x-speechcraft-csrf': auth.csrf_token } : {}),
      ...(options.headers || {}), ...(options.body ? { 'content-type': 'application/json' } : {}),
    } });

    stream = await openEvents(base, cookie, { origin: base });
    await stream.nextEvent();
    assert.ok((await apiRequest('/setup-api/start', { method: 'POST', body: '{}' })).ok);
    const sileroEvent = await readUntilEvent(stream,
      (item) => item.data.component_updates?.silero?.progress?.transferred_bytes === 500, 10_000);
    assert.equal(sileroEvent.data.component_updates.silero.progress.current_filename, 'model.onnx');
    assert.equal(sileroEvent.data.component_updates.silero.progress.artifact_name, 'silero_vad.onnx');
    assert.equal(sileroEvent.data.component_updates.silero.progress.percent, 50);
    const event = await readUntilEvent(stream,
      (item) => item.data.component_updates?.whisper?.progress?.transferred_bytes === 1_000_000, 10_000);
    assert.equal(event.data.component_updates.whisper.progress.role, 'asr');
    assert.equal(event.data.component_updates.whisper.progress.current_filename, 'model.bin');
    assert.equal(event.data.component_updates.whisper.progress.artifact_name, 'Systran/faster-whisper-large-v3');
    assert.equal(event.data.component_updates.whisper.progress.total_bytes, 2_000_000);
    const status = await apiRequest('/setup-api/status').then((response) => response.json());
    const models = await apiRequest('/setup-api/models').then((response) => response.json());
    assert.equal(status.components.whisper.progress.transferred_bytes, 1_000_000);
    assert.equal(models.models.find((model) => model.role === 'asr').progress.transferred_bytes, 1_000_000);
    assert.equal(status.components.silero.state, 'files_verified');
    await app.controller.waitForIdle();
    const streamAfterCompletion = await openEvents(base, cookie, { origin: base });
    try {
      const snapshot = await streamAfterCompletion.nextEvent();
      assert.equal(snapshot.event, 'snapshot');
      assert.equal(snapshot.data.components.whisper.progress, null,
        'completed files clear stale download speed and percent telemetry');
    } finally { streamAfterCompletion.close(); }
  } finally {
    stream?.close();
    await app.close();
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
