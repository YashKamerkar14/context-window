import Link from "next/link";
import { Suspense } from "react";

import { latestRun } from "@/lib/api";
import type { Briefing } from "@/lib/types";

import { StoryList } from "./story-list";

export function formatDate(iso: string): string {
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

const STATUS_TEXT: Record<string, string> = {
  running: "The newsroom is working on the next briefing",
  awaiting_review: "The next briefing is waiting for review",
  resume_requested: "The next briefing is being published",
  published: "Published",
  failed: "The last run failed",
};

async function RunStatus({ date }: { date: string }) {
  const run = await latestRun().catch(() => null);
  if (!run) return null;
  const live =
    run.status === "running" ||
    run.status === "awaiting_review" ||
    run.status === "resume_requested";
  const text =
    run.run_date === date && run.status === "published"
      ? "Published this morning"
      : (STATUS_TEXT[run.status] ?? run.status);
  return (
    <p className="mt-2 flex items-center gap-2 text-sm text-muted-foreground">
      <span
        className={`inline-block size-2 rounded-full ${live ? "animate-pulse bg-amber-500" : run.status === "failed" ? "bg-red-500" : "bg-emerald-500"}`}
        aria-hidden
      />
      {live ? (
        <Link
          href="/newsroom"
          className="underline underline-offset-2 hover:text-foreground"
        >
          {text}
        </Link>
      ) : (
        text
      )}
    </p>
  );
}

function Stat({ value, label }: { value: number; label: string }) {
  return (
    <div className="rounded-lg border border-border px-4 py-3">
      <p className="font-heading text-2xl tabular-nums">
        {value.toLocaleString("en-GB")}
      </p>
      <p className="text-xs text-muted-foreground">{label}</p>
    </div>
  );
}

export function BriefingView({
  briefing,
  latest,
}: {
  briefing: Briefing;
  latest: boolean;
}) {
  return (
    <main className="mx-auto w-full max-w-[720px] px-4 py-10 sm:px-6">
      <p className="text-sm text-muted-foreground">
        {latest ? "Today's briefing" : "Briefing"}
      </p>
      <h1 className="mt-1 font-heading text-3xl leading-tight sm:text-4xl">
        {formatDate(briefing.date)}
      </h1>
      {latest && (
        <Suspense fallback={<p className="mt-2 h-5 text-sm" />}>
          <RunStatus date={briefing.date} />
        </Suspense>
      )}
      <div className="mt-6 grid grid-cols-3 gap-2 sm:gap-3">
        <Stat value={briefing.stats.items_scanned} label="Items scanned" />
        <Stat value={briefing.stats.stories_kept} label="Stories kept" />
        <Stat value={briefing.stats.claims_verified} label="Claims verified" />
      </div>
      <p className="mt-6 text-lg leading-relaxed">{briefing.intro}</p>
      {briefing.light_day && (
        <p className="mt-3 rounded-lg border border-border px-4 py-3 text-sm text-muted-foreground">
          A light day: fewer than five stories cleared the bar, so this briefing
          is short rather than padded.
        </p>
      )}
      <StoryList stories={briefing.stories} />
      <nav className="mt-6 flex justify-between border-t border-border pt-6 text-sm">
        {briefing.previous_date ? (
          <Link
            href={`/briefings/${briefing.previous_date}`}
            className="text-muted-foreground hover:text-foreground"
          >
            ← {formatDate(briefing.previous_date)}
          </Link>
        ) : (
          <span />
        )}
        {briefing.next_date ? (
          <Link
            href={`/briefings/${briefing.next_date}`}
            className="text-muted-foreground hover:text-foreground"
          >
            {formatDate(briefing.next_date)} →
          </Link>
        ) : (
          <span />
        )}
      </nav>
    </main>
  );
}

export function NoBriefing({ message }: { message: string }) {
  return (
    <main className="mx-auto w-full max-w-[720px] px-4 py-16 sm:px-6">
      <h1 className="font-heading text-3xl">No briefing yet</h1>
      <p className="mt-4 text-muted-foreground">{message}</p>
    </main>
  );
}
