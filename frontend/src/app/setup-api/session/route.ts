import { proxySession } from "@/lib/setup-session-bridge";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return proxySession(request, false);
}

export async function POST(request: Request) {
  return proxySession(request, true);
}
