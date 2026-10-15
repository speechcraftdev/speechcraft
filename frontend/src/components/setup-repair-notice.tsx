"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { ensureSetupSession, SetupStatus, setupApi } from "@/lib/setup-api";

export function SetupRepairNotice() {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  useEffect(() => {
    let active = true;
    const abort = new AbortController();
    const refresh = async () => {
      try {
        await ensureSetupSession(false, abort.signal);
        if (!active) return;
        const value = await setupApi.status(abort.signal);
        if (active) setStatus(value);
      } catch { /* Setup may not be running in a manually started frontend. */ }
    };
    void refresh();
    const timer = setInterval(() => { void refresh(); }, 15_000);
    return () => { active = false; abort.abort(); clearInterval(timer); };
  }, []);

  if (!status?.onboarding?.completed || status.technical_processing_ready) return null;
  const blockers = status.components?.dataset_processing?.blockers || [];
  return <aside role="status" className="mb-5 rounded-lg border border-amber-700/20 bg-amber-50/70 p-4 text-left text-sm text-amber-950 dark:bg-amber-950/20 dark:text-amber-100">
    <p className="font-medium">SpeechCraft needs a runtime repair check.</p>
    {blockers.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5">{blockers.slice(0, 4).map((item) => <li key={`${item.component}:${item.code}`}>{item.message}</li>)}</ul>}
    {blockers.length === 0 && <p className="mt-1">The managed backend or a required runtime component is not currently healthy.</p>}
    <Link href="/setup" className="mt-2 inline-block underline underline-offset-4">Open setup and repair</Link>
  </aside>;
}
