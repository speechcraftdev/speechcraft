import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { createSetupController } from '../src/server.js';

export async function temporaryController(options = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'speechcraft-controller-test-'));
  const app = await createSetupController({ dataDirectory: directory, port: 0,
    simulated: { stepDelayMs: 0 }, ...options });
  const base = `http://127.0.0.1:${app.port}`;
  const origin = base;
  const bootstrap = fs.readFileSync(app.bootstrapPath, 'utf8').trim();
  const established = await fetch(`${base}/setup-api/session`, {
    method: 'POST', headers: { origin, authorization: `Bearer ${bootstrap}`,
      'content-type': 'application/json' }, body: '{}',
  });
  assert.equal(established.status, 201);
  const auth = await established.json();
  const setCookie = established.headers.get('set-cookie');
  const cookie = setCookie.split(';', 1)[0];

  async function request(endpoint, { method = 'GET', body, csrf = auth.csrf_token,
    cookieValue = cookie, originValue = origin, headers = {} } = {}) {
    const requestHeaders = { ...headers };
    if (originValue) requestHeaders.origin = originValue;
    if (cookieValue) requestHeaders.cookie = cookieValue;
    if (csrf) requestHeaders['x-speechcraft-csrf'] = csrf;
    if (body !== undefined) {
      requestHeaders['content-type'] ??= 'application/json';
    }
    return fetch(`${base}${endpoint}`, { method, headers: requestHeaders,
      body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) });
  }

  return { app, base, origin, cookie, setCookie, auth, request, directory };
}

export async function waitFor(predicate, { timeoutMs = 3_000, intervalMs = 5 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await delay(intervalMs);
  }
  assert.fail(`condition was not reached within ${timeoutMs}ms`);
}

export async function openEvents(base, cookie, { lastEventId, origin } = {}) {
  const headers = { accept: 'text/event-stream', cookie };
  if (lastEventId !== undefined) headers['last-event-id'] = String(lastEventId);
  if (origin) headers.origin = origin;
  const abort = new AbortController();
  const response = await fetch(`${base}/setup-api/events`, { headers, signal: abort.signal });
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  let buffered = '';
  const queued = [];
  return {
    abort,
    reader,
    async nextEvent() {
      if (queued.length) return queued.shift();
      while (true) {
        const { value, done } = await reader.read();
        if (done) throw new Error('SSE stream ended before the expected event');
        buffered += new TextDecoder().decode(value);
        const frames = buffered.split('\n\n');
        buffered = frames.pop();
        for (const frame of frames) {
          if (!frame.trim() || frame.startsWith(':')) continue;
          const parsed = { id: null, event: 'message', data: '' };
          for (const line of frame.split('\n')) {
            if (line.startsWith('id: ')) parsed.id = Number(line.slice(4));
            else if (line.startsWith('event: ')) parsed.event = line.slice(7);
            else if (line.startsWith('data: ')) parsed.data += line.slice(6);
          }
          parsed.data = JSON.parse(parsed.data || 'null');
          queued.push(parsed);
        }
        if (queued.length) return queued.shift();
      }
    },
    close() { abort.abort(); },
  };
}

export async function readUntilEvent(client, predicate, timeoutMs = 3_000) {
  let timer;
  try {
    return await Promise.race([
      (async () => {
        while (true) {
          const event = await client.nextEvent();
          if (predicate(event)) return event;
        }
      })(),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('timed out waiting for SSE event')), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
