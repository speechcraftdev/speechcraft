import { redirect } from "next/navigation";
import { legacyOnboardingRedirect } from "@/lib/onboarding-routes.mjs";

export async function LegacyOnboardingRedirect({
  path,
  searchParams,
}: {
  path: string;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  redirect(legacyOnboardingRedirect(path, await searchParams));
}
