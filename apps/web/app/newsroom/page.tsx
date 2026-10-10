import type { Metadata } from "next";

import { Floor } from "@/components/floor/floor";

export const metadata: Metadata = {
  title: "The newsroom floor · Context Window",
  description:
    "Watch the AI agents that report, fact-check and write the briefing at work.",
};

export default function NewsroomPage() {
  return <Floor />;
}
