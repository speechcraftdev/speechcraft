import "server-only";

import fs from "node:fs";
import path from "node:path";

type BridgeConfig = { controller: URL; origins: Set<string>; hosts: Set<string>; tokenFile: string };

function loopback(hostname: string) {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "[::1]" || hostname === "::1";
}

function config(): BridgeConfig | null {
  if (process.env.SPEECHCRAFT_SETUP_SESSION_BRIDGE !== "1") return null;
  // The launcher is responsible for binding Next.js to 127.0.0.1. Requiring
  // its explicit confirmation prevents accidentally enabling this privileged
  // bootstrap bridge in a network-facing deployment.
  if (process.env.SPEECHCRAFT_SETUP_BRIDGE_LOOPBACK_CONFIRMED !== "1") return null;
  const rawController = process.env.SPEECHCRAFT_SETUP_CONTROLLER_URL || "http://127.0.0.1:4317";
  const rawOrigins = process.env.SPEECHCRAFT_SETUP_ALLOWED_APP_ORIGINS;
  const tokenFile = process.env.SPEECHCRAFT_SETUP_BOOTSTRAP_FILE;
  if (!rawOrigins || !tokenFile || !path.isAbsolute(tokenFile)) return null;
  let controller: URL;
  try { controller = new URL(rawController); } catch { return null; }
  if (controller.protocol !== "http:" || !loopback(controller.hostname) || controller.username || controller.password
      || controller.search || controller.hash || (controller.pathname !== "/" && controller.pathname !== "")) return null;
  const origins = new Set<string>();
  const hosts = new Set<string>();
  for (const entry of rawOrigins.split(",").map((value) => value.trim()).filter(Boolean)) {
    try {
      const origin = new URL(entry);
      if (origin.origin !== entry || !loopback(origin.hostname)) return null;
      origins.add(origin.origin);
      hosts.add(origin.host.toLowerCase());
    } catch { return null; }
  }
  if (origins.size === 0) return null;
  return { controller, origins, hosts, tokenFile };
}

export function validateBridgeRequest(request: Request) {
  const settings = config();
  if (!settings) return { ok: false as const, status: 503, message: "The local setup session bridge is not configured." };
  if (new URL(request.url).search) return { ok: false as const, status: 400, message: "Setup credentials and options are never accepted in URLs." };
  const host = request.headers.get("host")?.toLowerCase();
  if (!host || !settings.hosts.has(host)) return { ok: false as const, status: 400, message: "This application host is not allowed." };
  if (request.headers.get("sec-fetch-site") === "cross-site") return { ok: false as const, status: 403, message: "Cross-site setup requests are not allowed." };
  const origin = request.headers.get("origin");
  if (request.method === "POST" && (!origin || !settings.origins.has(origin))) {
    return { ok: false as const, status: 403, message: "Open SpeechCraft from its configured local address and retry." };
  }
  if (origin && !settings.origins.has(origin)) return { ok: false as const, status: 403, message: "This request origin is not allowed." };
  return { ok: true as const, settings };
}

function readBootstrapCredential(filename: string): Buffer {
  const absolute = path.resolve(filename);
  if (path.basename(absolute) !== "bootstrap.token") throw new Error("The local setup credential path is not the controller bootstrap file.");
  const info = fs.lstatSync(absolute);
  if (!info.isFile() || info.isSymbolicLink() || (typeof process.getuid === "function" && info.uid !== process.getuid())
      || (info.mode & 0o077) !== 0 || info.size > 256 || fs.realpathSync(absolute) !== absolute) {
    throw new Error("The local setup credential file is not a private regular file.");
  }
  const parent = fs.lstatSync(path.dirname(absolute));
  if (!parent.isDirectory() || parent.isSymbolicLink() || (parent.mode & 0o077) !== 0
      || (typeof process.getuid === "function" && parent.uid !== process.getuid())) {
    throw new Error("The local setup credential directory is not private.");
  }
  const secret = fs.readFileSync(absolute);
  const value = secret.toString("ascii").trim();
  if (!/^[-_A-Za-z0-9]{40,64}$/.test(value)) {
    secret.fill(0);
    throw new Error("The local setup credential file has an invalid format.");
  }
  secret.fill(0);
  return Buffer.from(value, "ascii");
}

function noStoreHeaders() {
  return { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" };
}

async function readBoundedBody(request: Request, limit: number): Promise<string | null> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(body);
}

async function readBoundedResponse(response: Response, limit: number): Promise<string | null> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
  return new TextDecoder().decode(body);
}

function validSessionBody(value: unknown): value is { csrf_token: string; expires_at: string } {
  if (!value || typeof value !== "object") return false;
  const session = value as Record<string, unknown>;
  return typeof session.csrf_token === "string" && /^[A-Za-z0-9_-]{43}$/.test(session.csrf_token)
    && typeof session.expires_at === "string" && Number.isFinite(Date.parse(session.expires_at));
}

export async function proxySession(request: Request, establish: boolean) {
  const checked = validateBridgeRequest(request);
  if (!checked.ok) return Response.json({ error: { code: "SESSION_BRIDGE_REJECTED", message: checked.message } }, { status: checked.status, headers: noStoreHeaders() });
  const { settings } = checked;
  let authorization: string | undefined;
  let credential: Buffer | undefined;
  try {
    if (establish) {
      const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase();
      const rawLength = request.headers.get("content-length");
      const length = rawLength === null ? 0 : Number(rawLength);
      if (contentType !== "application/json" || !Number.isSafeInteger(length) || length < 0) {
        return Response.json({ error: { code: "INVALID_REQUEST", message: "Session establishment accepts only an empty JSON object." } }, { status: 400, headers: noStoreHeaders() });
      }
      if (length > 128) return Response.json({ error: { code: "BODY_TOO_LARGE", message: "Session request body exceeds 128 bytes." } }, { status: 413, headers: noStoreHeaders() });
      const raw = await readBoundedBody(request, 128);
      if (raw === null) return Response.json({ error: { code: "BODY_TOO_LARGE", message: "Session request body exceeds 128 bytes." } }, { status: 413, headers: noStoreHeaders() });
      if (!["", "{}"].includes(raw.trim())) {
        return Response.json({ error: { code: "INVALID_REQUEST", message: "Session establishment accepts only an empty JSON object." } }, { status: 400, headers: noStoreHeaders() });
      }
      credential = readBootstrapCredential(settings.tokenFile);
      authorization = `Bearer ${credential.toString("ascii")}`;
    }
    const controllerCookie = request.headers.get("cookie");
    const response = await fetch(`${settings.controller.origin}/setup-api/session`, {
      method: establish ? "POST" : "GET",
      headers: {
        origin: settings.controller.origin,
        ...(authorization ? { authorization } : {}),
        ...(controllerCookie ? { cookie: controllerCookie } : {}),
        ...(establish ? { "content-type": "application/json" } : {}),
      },
      ...(establish ? { body: "{}" } : {}),
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(5000),
    });
    const raw = await readBoundedResponse(response, 4096);
    if (raw === null) return Response.json({ error: { code: "SESSION_RESPONSE_INVALID", message: "The local setup controller returned an invalid session response." } }, { status: 502, headers: noStoreHeaders() });
    let payload: unknown;
    try { payload = JSON.parse(raw); } catch { payload = null; }
    if ((establish && response.status === 201) || (!establish && response.status === 200)) {
      if (!validSessionBody(payload)) return Response.json({ error: { code: "SESSION_RESPONSE_INVALID", message: "The local setup controller returned an invalid session response." } }, { status: 502, headers: noStoreHeaders() });
      const headers = new Headers(noStoreHeaders());
      if (establish) {
        const setCookie = response.headers.get("set-cookie");
        if (!setCookie || !/^speechcraft_setup_session=[A-Za-z0-9_-]{43};\s*Path=\/setup-api;\s*HttpOnly;\s*SameSite=Strict;\s*Max-Age=\d+(?:;\s*Secure)?$/i.test(setCookie)) {
          return Response.json({ error: { code: "SESSION_RESPONSE_INVALID", message: "The local setup controller returned an invalid session cookie." } }, { status: 502, headers: noStoreHeaders() });
        }
        headers.set("Set-Cookie", setCookie);
      }
      return Response.json({ csrf_token: payload.csrf_token, expires_at: payload.expires_at }, { status: response.status, headers });
    }
    const status = !establish && response.status === 401 ? 401 : response.status >= 500 ? 503 : 403;
    const code = !establish && response.status === 401 ? "AUTH_REQUIRED" : "SESSION_ESTABLISHMENT_FAILED";
    const message = !establish && response.status === 401 ? "Establish a local setup session first."
      : "The local setup controller rejected session establishment.";
    return Response.json({ error: { code, message } }, { status, headers: noStoreHeaders() });
  } catch {
    return Response.json({ error: { code: "SETUP_CONTROLLER_UNAVAILABLE", message: "SpeechCraft could not establish a setup session. Check that the local setup controller is running and its private session configuration is available." } }, { status: 503, headers: noStoreHeaders() });
  } finally {
    credential?.fill(0);
    authorization = undefined;
  }
}
