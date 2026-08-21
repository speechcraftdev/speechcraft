const configuredBase = process.env.NEXT_PUBLIC_SPEECHCRAFT_API_URL?.trim();

export function speechcraftApiBase(): string {
  if (configuredBase) {
    return configuredBase.replace(/\/+$/, "");
  }
  if (typeof window !== "undefined") {
    const protocol = window.location.protocol === "https:" ? "https:" : "http:";
    const host = window.location.hostname || "127.0.0.1";
    return `${protocol}//${host}:8010`;
  }
  return "http://127.0.0.1:8010";
}
