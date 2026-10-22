import type { Metadata } from "next";
import { TranscriptionProgress } from "@/components/transcription-progress";

export const metadata: Metadata = { title: "Processing | Speechcraft" };

export default function Page() {
  return <TranscriptionProgress />;
}
