"use client";

import { SubmitButton } from "@midday/ui/submit-button";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { demoEnabled, withDemo } from "@/lib/demo";

// End of the setup wizard: hand off into the Lab workstation. No auth
// return_to concept anymore (local single-user tool).
export function LoginContinueButton() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [isLoading, setLoading] = useState(false);

  const handleContinue = () => {
    setLoading(true);
    const target = new URLSearchParams();
    const project = searchParams.get("project");
    const run = searchParams.get("run");
    if (project) target.set("project", project);
    if (run) target.set("run", run);
    const path = target.size ? `/lab?${target.toString()}` : "/lab";
    router.push(withDemo(path, demoEnabled(searchParams)));
  };

  return (
    <SubmitButton
      type="button"
      onClick={handleContinue}
      className="bg-primary px-6 py-4 text-secondary font-medium flex space-x-2 h-[40px] w-full"
      isSubmitting={isLoading}
    >
      Continue
    </SubmitButton>
  );
}
