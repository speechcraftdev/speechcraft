import type { Metadata } from "next";
import { VoiceSelection } from "@/components/voice-selection";

export const metadata: Metadata = { title: "Choose speaker | Speechcraft" };

export default function Page() {
  return <VoiceSelection />;
}
