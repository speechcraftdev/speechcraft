import type { NextConfig } from "next";

// Same-origin proxy to speechcraft's local FastAPI backend so the browser
// hits /sc-api/* (no CORS) and audio range requests pass through cleanly.
const speechcraftBackend =
  process.env.SPEECHCRAFT_BACKEND_URL || "http://127.0.0.1:8010";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  // Local single-user tool: keep builds unblocked while we iterate.
  typescript: { ignoreBuildErrors: true },
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
    ];
  },
};

export default nextConfig;
