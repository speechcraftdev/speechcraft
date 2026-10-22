import type { Metadata } from "next";
import { SpeakerMode } from "@/components/speaker-mode";

export const metadata: Metadata = { title: "Choose speakers | Speechcraft" };

export default function Page() {
  return <SpeakerMode />;
}
