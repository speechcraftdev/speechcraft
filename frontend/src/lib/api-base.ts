const configuredBase = process.env.NEXT_PUBLIC_SPEECHCRAFT_API_URL?.trim();

export function speechcraftApiBase(): string {
  if (configuredBase) {
    return configuredBase.replace(/\/+$/, "");
  }
  // The Next server owns the loopback backend address and proxies it through
  // /sc-api. Keeping browser requests same-origin also works through SSH and
  // staging tunnels where the backend port is not exposed to the browser.
  return "/sc-api";
}
