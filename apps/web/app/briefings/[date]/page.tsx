import { notFound } from "next/navigation";
import { Suspense } from "react";

import { BriefingView } from "@/components/briefing-view";
import { briefingFor } from "@/lib/api";

async function DatedBriefing({ date }: { date: string }) {
  const briefing = await briefingFor(date);
  if (!briefing) notFound();
  return <BriefingView briefing={briefing} latest={false} />;
}

// The date is only known at request time, so it is read inside Suspense (Cache Components).
export default function BriefingPage({
  params,
}: PageProps<"/briefings/[date]">) {
  return (
    <Suspense
      fallback={
        <main className="mx-auto w-full max-w-[1100px] px-4 py-8 sm:px-6" />
      }
    >
      {params.then(({ date }) => (
        <DatedBriefing date={date} />
      ))}
    </Suspense>
  );
}
