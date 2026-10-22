import { LegacyOnboardingRedirect } from "../legacy-onboarding-redirect";

export default async function Page({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  return await LegacyOnboardingRedirect({ path: "/login3", searchParams });
}
