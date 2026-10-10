import Link from "next/link";
import { Suspense } from "react";

import { latestRun } from "@/lib/api";
import type { Briefing } from "@/lib/types";

import { NewsroomStrip } from "./floor/strip";
import { CountUp } from "./reveal";
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
  running: "The newsroom is working on the next edition",
  awaiting_review: "The next edition is waiting for review",
  resume_requested: "The next edition is being published",
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
    <span className="flex items-center gap-2">
      <span
        className={`inline-block size-2 rounded-full ${live ? "cw-live bg-red-500" : run.status === "failed" ? "bg-red-500" : "bg-emerald-500"}`}
        aria-hidden
      />
      {live ? (
        <Link
          href="/newsroom"
          className="underline underline-offset-2 hover:text-foreground"
        >
          {text}: watch live
        </Link>
      ) : (
        text
      )}
    </span>
  );
}

function Masthead({
  briefing,
  latest,
}: {
  briefing: Briefing;
  latest: boolean;
}) {
  const { items_scanned, stories_kept, claims_verified } = briefing.stats;
  return (
    <header>
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 border-b border-border pb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {latest ? (
          <Suspense fallback={<span />}>
            <RunStatus date={briefing.date} />
          </Suspense>
        ) : (
          <Link href="/" className="hover:text-foreground">
            From the archive · go to today&apos;s edition →
          </Link>
        )}
        <span>The daily AI briefing for engineers</span>
      </div>
      <p className="py-5 text-center font-heading text-5xl leading-none tracking-tight sm:py-7 sm:text-7xl lg:text-8xl">
        Context Window
      </p>
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-y-4 border-double border-foreground py-3 text-sm">
        <h1 className="font-heading text-lg font-semibold sm:text-xl">
          {formatDate(briefing.date)}
        </h1>
        <p className="flex flex-wrap items-center gap-x-2 text-muted-foreground">
          <span>
            <span className="font-semibold text-foreground">
              <CountUp value={items_scanned} />
            </span>{" "}
            items read
          </span>
          <span aria-hidden>▸</span>
          <span>
            <span className="font-semibold text-foreground">
              <CountUp value={claims_verified} />
            </span>{" "}
            claims verified
          </span>
          <span aria-hidden>▸</span>
          <span>
            <span className="font-semibold text-foreground">
              <CountUp value={stories_kept} />
            </span>{" "}
            stories made the cut
          </span>
        </p>
        <NewsroomStrip />
      </div>
    </header>
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
    <main className="mx-auto w-full max-w-[1100px] px-4 py-6 sm:px-6 sm:py-8">
      <Masthead briefing={briefing} latest={latest} />
      <section className="mt-8 grid gap-4 lg:grid-cols-[180px_minmax(0,1fr)] lg:gap-10">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground lg:pt-2">
          The day in brief
        </p>
        <div>
          <p className="max-w-[68ch] text-xl leading-relaxed first-letter:float-left first-letter:mt-1 first-letter:mr-2 first-letter:font-heading first-letter:text-6xl first-letter:leading-[0.85] first-letter:font-semibold">
            {briefing.intro}
          </p>
          {briefing.light_day && (
            <p className="mt-4 max-w-[68ch] rounded-lg border border-border px-4 py-3 text-sm text-muted-foreground">
              A light day: fewer than five stories cleared the bar, so this
              edition is short rather than padded.
            </p>
          )}
        </div>
      </section>
      <StoryList stories={briefing.stories} />
      <nav className="mt-6 flex justify-between gap-4 border-t-4 border-double border-foreground pt-6 text-sm">
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
            className="text-right text-muted-foreground hover:text-foreground"
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
