import type { Metadata } from "next";

import { Newsroom } from "@/components/newsroom/newsroom";

export const metadata: Metadata = { title: "Newsroom · Context Window" };

export default function NewsroomPage() {
  return <Newsroom />;
}
