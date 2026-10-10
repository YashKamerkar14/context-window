import type { Metadata } from "next";
import Link from "next/link";

import { formatDate } from "@/components/briefing-view";
import { archive } from "@/lib/api";

export const metadata: Metadata = { title: "Archive · Context Window" };

export default async function ArchivePage() {
  const briefings = await archive();
  return (
    <main className="mx-auto w-full max-w-[720px] px-4 py-10 sm:px-6">
      <h1 className="font-heading text-3xl sm:text-4xl">Archive</h1>
      {briefings.length === 0 ? (
        <p className="mt-6 text-muted-foreground">
          No briefings have been published yet.
        </p>
      ) : (
        <ul className="mt-6">
          {briefings.map((b) => (
            <li key={b.date} className="border-t border-border py-4">
              <Link href={`/briefings/${b.date}`} className="group block">
                <p className="font-heading text-lg group-hover:underline">
                  {formatDate(b.date)}
                </p>
                <p className="mt-1 text-sm text-muted-foreground">
                  {b.stories} {b.stories === 1 ? "story" : "stories"}
                  {b.light_day ? " · a light day" : ""}
                </p>
                {b.intro && (
                  <p className="mt-1 line-clamp-2 text-sm">{b.intro}</p>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
