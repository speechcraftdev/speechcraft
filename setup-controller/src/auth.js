import { randomBytes, timingSafeEqual } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { atomicWriteFile } from './state-store.js';

const COOKIE_NAME = 'speechcraft_setup_session';
const SESSION_TTL_MS = 8 * 60 * 60 * 1000;
const SESSION_LIMIT = 32;

export class SessionAuth {
  constructor(dataDirectory, { secureCookies = false, now = Date.now } = {}) {
    this.bootstrapPath = path.join(dataDirectory, 'bootstrap.token');
    this.bootstrapSecret = randomBytes(32).toString('base64url');
    this.secureCookies = secureCookies;
    this.now = now;
    this.sessions = new Map();
    atomicWriteFile(dataDirectory, 'bootstrap.token', `${this.bootstrapSecret}\n`, 0o600);
  }

  bootstrapMatches(header) {
    if (typeof header !== 'string' || !header.startsWith('Bearer ')) return false;
    const provided = Buffer.from(header.slice(7));
    const expected = Buffer.from(this.bootstrapSecret);
    return provided.length === expected.length && timingSafeEqual(provided, expected);
  }

  establish(header) {
    if (!this.bootstrapMatches(header)) return null;
    for (const [id, session] of this.sessions) {
      if (session.expiresAt <= this.now()) this.sessions.delete(id);
    }
    while (this.sessions.size >= SESSION_LIMIT) this.sessions.delete(this.sessions.keys().next().value);
    const id = randomBytes(32).toString('base64url');
    const csrf = randomBytes(32).toString('base64url');
    const expiresAt = this.now() + SESSION_TTL_MS;
    this.sessions.set(id, { csrf, expiresAt });
    return {
      csrf_token: csrf,
      expires_at: new Date(expiresAt).toISOString(),
      set_cookie: `${COOKIE_NAME}=${id}; Path=/setup-api; HttpOnly; SameSite=Strict; Max-Age=${SESSION_TTL_MS / 1000}${this.secureCookies ? '; Secure' : ''}`,
    };
  }

  sessionFromRequest(request) {
    const cookieHeader = request.headers.cookie || '';
    const pair = cookieHeader.split(';').map((value) => value.trim())
      .find((value) => value.startsWith(`${COOKIE_NAME}=`));
    if (!pair) return null;
    const id = pair.slice(COOKIE_NAME.length + 1);
    const session = this.sessions.get(id);
    if (!session || session.expiresAt <= this.now()) {
      this.sessions.delete(id);
      return null;
    }
    return { id, ...session };
  }

  csrfMatches(session, header) {
    if (!session || typeof header !== 'string') return false;
    const provided = Buffer.from(header);
    const expected = Buffer.from(session.csrf);
    return provided.length === expected.length && timingSafeEqual(provided, expected);
  }

  close() {
    this.sessions.clear();
    try {
      const info = fs.lstatSync(this.bootstrapPath);
      if (info.isFile() && !info.isSymbolicLink()) fs.unlinkSync(this.bootstrapPath);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
}
