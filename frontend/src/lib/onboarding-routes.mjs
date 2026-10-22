export const LEGACY_ONBOARDING_ROUTES = Object.freeze({
  "/login": "/onboarding/welcome",
  "/login2": "/onboarding/import",
  "/login3": "/onboarding/setup",
  "/login4": "/onboarding/choose-speaker",
  "/login5": "/onboarding/processing",
  "/login6": "/lab",
});

export function legacyOnboardingRedirect(path, params = {}) {
  const destination = LEGACY_ONBOARDING_ROUTES[path];
  if (!destination) throw new Error(`Unknown legacy onboarding route: ${path}`);
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (Array.isArray(value)) value.forEach((item) => query.append(key, item));
    else if (value !== undefined) query.set(key, value);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return `${destination}${suffix}`;
}
