import type { NextConfig } from "next";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// Same-origin proxy to speechcraft's local FastAPI backend so the browser
// hits /sc-api/* (no CORS) and audio range requests pass through cleanly.
const speechcraftBackend =
  process.env.SPEECHCRAFT_BACKEND_URL || "http://127.0.0.1:8010";
const backendUrl = new URL(speechcraftBackend);
if (backendUrl.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(backendUrl.hostname) ||
    backendUrl.username || backendUrl.password || backendUrl.search || backendUrl.hash) {
  throw new Error("SPEECHCRAFT_BACKEND_URL must identify an HTTP loopback backend.");
}
// Keep the controller address server-side. Browser code always uses the
// same-origin /setup-api path and cannot choose this destination.
const setupController =
  process.env.SPEECHCRAFT_SETUP_CONTROLLER_URL || "http://127.0.0.1:4317";
const setupControllerUrl = new URL(setupController);
if (setupControllerUrl.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(setupControllerUrl.hostname) ||
    setupControllerUrl.username || setupControllerUrl.password || setupControllerUrl.search || setupControllerUrl.hash ||
    !["", "/"].includes(setupControllerUrl.pathname)) {
  throw new Error("SPEECHCRAFT_SETUP_CONTROLLER_URL must identify a local loopback HTTP controller.");
}

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: repositoryRoot,
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  experimental: {
    // Local Speechcraft ingest routinely streams large WAVs through the /sc-api
    // rewrite. The default 10MB proxy buffer truncates uploads.
    proxyClientMaxBodySize: "512mb",
  },
  async rewrites() {
    return [
      {
        source: "/sc-api/:path*",
        destination: `${speechcraftBackend}/:path*`,
      },
      {
        source: "/setup-api/:path*",
        destination: `${setupControllerUrl.origin}/setup-api/:path*`,
      },
    ];
  },
};

export default nextConfig;
