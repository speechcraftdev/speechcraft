"use client";

import type { ReactNode } from "react";

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }): ReactNode {
  return (
    <html lang="en">
      <body>
        <main role="alert" style={{ maxWidth: 640, margin: "12vh auto", padding: 24, fontFamily: "sans-serif" }}>
          <h1>SpeechCraft needs a moment</h1>
          <p>The application encountered an unexpected error.</p>
          <button onClick={reset}>Try again</button>
        </main>
      </body>
    </html>
  );
}
