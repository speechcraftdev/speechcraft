import { createHmac } from "node:crypto";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function noStore() {
  return { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer" };
}

export async function POST(request: Request) {
  const configured = process.env.SPEECHCRAFT_LAUNCHER_PRIVATE_IDENTITY;
  const length = Number(request.headers.get("content-length") || "0");
  if (request.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json"
      || !Number.isSafeInteger(length) || length < 1 || length > 128
      || !configured || !/^[A-Za-z0-9_-]{43}$/.test(configured)) {
    return new Response(null, { status: 404, headers: noStore() });
  }
  let body: unknown;
  try { body = await request.json(); } catch { return new Response(null, { status: 404, headers: noStore() }); }
  if (!body || typeof body !== "object" || Object.keys(body).length !== 1
      || typeof (body as { challenge?: unknown }).challenge !== "string"
      || !/^[A-Za-z0-9_-]{43}$/.test((body as { challenge: string }).challenge)) {
    return new Response(null, { status: 404, headers: noStore() });
  }
  const key = Buffer.from(configured, "base64url");
  const proof = createHmac("sha256", key).update((body as { challenge: string }).challenge).digest("base64url");
  key.fill(0);
  return Response.json({ proof }, { headers: noStore() });
}
