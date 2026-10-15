import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET() {
  const installationId = process.env.SPEECHCRAFT_LAUNCHER_INSTALL_ID;
  if (!installationId || !/^[a-f0-9]{64}$/.test(installationId)) {
    return NextResponse.json({ error: "launcher_not_configured" }, {
      status: 503,
      headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
    });
  }
  return NextResponse.json({ service: "speechcraft-frontend", installation_id: installationId }, {
    headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}
