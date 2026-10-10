import { BriefingView, NoBriefing } from "@/components/briefing-view";
import { latestBriefing } from "@/lib/api";

export default async function Home() {
  const briefing = await latestBriefing();
  if (!briefing) {
    return (
      <NoBriefing message="The newsroom has not published a briefing yet. It runs every morning." />
    );
  }
  return <BriefingView briefing={briefing} latest />;
}
