"use client";

import { useMemo, useState } from "react";

import { useAdmin } from "@/hooks/use-admin";
import { AGENTS } from "@/lib/floor";
import { BEATS, type Beat, type Story } from "@/lib/types";

import { BEAT_DOT, BEAT_LABEL, BEAT_STYLE } from "./beats";
import { Reveal, Stamp } from "./reveal";

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** Every distinct page a story was checked against: its sources and its evidence. */
function checkedSources(story: Story): string[] {
  const urls = [
    ...story.source_urls,
    ...story.claims.map((c) => c.evidence_url ?? c.source_url),
  ];
  return [...new Set(urls)];
}

function supportedClaims(story: Story): number {
  return story.claims.filter((c) => c.verdict === "supported").length;
}

function Feedback({ storyId }: { storyId: string }) {
  const [vote, setVote] = useState<1 | -1 | null>(null);
  const [busy, setBusy] = useState(false);
  async function send(value: 1 | -1) {
    setBusy(true);
    try {
      const response = await fetch(`/api/admin/stories/${storyId}/feedback`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ value }),
      });
      if (response.ok) setVote(value);
    } finally {
      setBusy(false);
    }
  }
  const button = (value: 1 | -1, label: string, text: string) => (
    <button
      type="button"
      disabled={busy}
      onClick={() => send(value)}
      aria-pressed={vote === value}
      aria-label={label}
      className={`rounded-md border px-2 py-0.5 text-xs ${
        vote === value
          ? "border-foreground text-foreground"
          : "border-border text-muted-foreground hover:text-foreground"
      }`}
    >
      {text}
    </button>
  );
  return (
    <div className="flex items-center gap-2">
      {button(1, "More like this", "▲ More")}
      {button(-1, "Less like this", "▼ Less")}
    </div>
  );
}

/** The claims behind a story, each with the evidence the fact desk found. */
function Sources({ story }: { story: Story }) {
  const sources = checkedSources(story);
  return (
    <details className="group text-sm">
      <summary className="cursor-pointer list-none text-muted-foreground hover:text-foreground">
        <span className="group-open:hidden">▸</span>
        <span className="hidden group-open:inline">▾</span> How we know:{" "}
        {sources.length} {sources.length === 1 ? "source" : "sources"}
      </summary>
      <ul className="mt-3 space-y-3 border-l-2 border-emerald-500/40 pl-4">
        {story.claims.map((claim) => (
          <li key={claim.id}>
            <p>
              {claim.verdict === "supported" && (
                <span
                  className="mr-1.5 text-emerald-600 dark:text-emerald-400"
                  aria-label="Supported"
                >
                  ✓
                </span>
              )}
              {claim.text}
            </p>
            {claim.evidence_quote && (
              <blockquote className="mt-1 text-muted-foreground">
                “{claim.evidence_quote}”
              </blockquote>
            )}
            <a
              href={claim.evidence_url ?? claim.source_url}
              className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground"
              target="_blank"
              rel="noreferrer"
            >
              {host(claim.evidence_url ?? claim.source_url)}
              {claim.evidence_kind === "feed" ? " (from the feed)" : ""}
            </a>
          </li>
        ))}
      </ul>
      <p className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-xs">
        {sources.map((url) => (
          <a
            key={url}
            href={url}
            target="_blank"
            rel="noreferrer"
            className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
          >
            {host(url)}
          </a>
        ))}
      </p>
    </details>
  );
}

function Kicker({ story }: { story: Story }) {
  return (
    <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs font-semibold uppercase tracking-wide">
      <span className={`flex items-center gap-1.5 ${BEAT_STYLE[story.beat]}`}>
        <span
          className={`size-2 rounded-full ${BEAT_DOT[story.beat]}`}
          aria-hidden
        />
        {BEAT_LABEL[story.beat]}
      </span>
      {story.is_breaking_change && (
        <span className="rounded-sm bg-red-600 px-1.5 py-0.5 text-[10px] text-white">
          Breaking change
        </span>
      )}
    </p>
  );
}

/** Who made the story: the beat's reporter, the fact desk and, if they edited it, you. */
function Byline({ story }: { story: Story }) {
  const people = [
    { agent: AGENTS[story.beat], verb: "Reported by" },
    { agent: AGENTS.factdesk, verb: "Checked by" },
    ...(story.edited_by_editor
      ? [{ agent: AGENTS.you, verb: "Edited by" }]
      : []),
  ];
  return (
    <p className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
      {people.map(({ agent, verb }) => (
        <span key={agent.id} className="flex items-center gap-1.5">
          <span
            className={`grid size-6 place-items-center rounded-full text-sm ring-1 ${agent.tone}`}
            aria-hidden
          >
            {agent.avatar}
          </span>
          {verb}
          <span className="font-medium text-foreground">
            {agent.id === "you" ? "the human editor" : agent.name}
          </span>
        </span>
      ))}
    </p>
  );
}

function LeadStory({ story, admin }: { story: Story; admin: boolean }) {
  return (
    <Reveal>
      <article className="border-t-4 border-foreground pt-5">
        <Kicker story={story} />
        <div className="mt-3 flex items-start justify-between gap-6">
          <h2 className="font-heading text-3xl leading-[1.1] tracking-tight sm:text-5xl">
            {story.headline}
          </h2>
          <div className="sm:hidden">
            <Stamp
              supported={supportedClaims(story)}
              total={story.claims.length}
            />
          </div>
          <div className="hidden sm:block">
            <Stamp
              supported={supportedClaims(story)}
              total={story.claims.length}
              large
            />
          </div>
        </div>
        <div className="mt-5 grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px] lg:gap-10">
          <div>
            <p className="text-lg leading-relaxed">{story.summary}</p>
            <div className="mt-5">
              <Byline story={story} />
            </div>
          </div>
          <aside className="rounded-xl bg-muted/60 p-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Why it matters
            </p>
            <p className="mt-2 leading-relaxed">{story.why_it_matters}</p>
          </aside>
        </div>
        <div className="mt-4 flex flex-wrap items-start justify-between gap-3">
          <Sources story={story} />
          {admin && <Feedback storyId={story.id} />}
        </div>
      </article>
    </Reveal>
  );
}

function StoryCard({ story, admin }: { story: Story; admin: boolean }) {
  return (
    <Reveal>
      <article className="border-t border-border pt-5 pb-8">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <Kicker story={story} />
            <h3 className="mt-2 font-heading text-xl leading-snug sm:text-2xl">
              {story.headline}
            </h3>
          </div>
          <Stamp
            supported={supportedClaims(story)}
            total={story.claims.length}
          />
        </div>
        <p className="mt-3 leading-relaxed">{story.summary}</p>
        <p className="mt-3 border-l-2 border-border pl-3 leading-relaxed text-muted-foreground">
          <span className="font-medium text-foreground">Why it matters: </span>
          {story.why_it_matters}
        </p>
        <div className="mt-4 space-y-3">
          <Byline story={story} />
          <div className="flex flex-wrap items-start justify-between gap-3">
            <Sources story={story} />
            {admin && <Feedback storyId={story.id} />}
          </div>
        </div>
      </article>
    </Reveal>
  );
}

/** The front page: section tabs, the lead story, then the rest in two columns. */
export function StoryList({ stories }: { stories: Story[] }) {
  const admin = useAdmin();
  const [beat, setBeat] = useState<Beat | "all">("all");
  const present = useMemo(
    () => BEATS.filter((b) => stories.some((s) => s.beat === b)),
    [stories],
  );
  const shown =
    beat === "all" ? stories : stories.filter((s) => s.beat === beat);
  // A section shows its stories in the grid; the front page leads with the top story.
  const lead = beat === "all" ? shown[0] : undefined;
  const rest = beat === "all" ? shown.slice(1) : shown;
  const tab = (value: Beat | "all", label: string, count: number) => (
    <button
      key={value}
      type="button"
      onClick={() => setBeat(value)}
      aria-pressed={beat === value}
      className={`-mb-px flex items-center gap-1.5 border-b-2 px-1 pb-2 text-sm transition-colors ${
        beat === value
          ? "border-foreground text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground"
      }`}
    >
      {value !== "all" && (
        <span
          className={`size-2 rounded-full ${BEAT_DOT[value]}`}
          aria-hidden
        />
      )}
      {label}
      <span className="text-xs tabular-nums text-muted-foreground">
        {count}
      </span>
    </button>
  );
  return (
    <section className="mt-10">
      <div
        className="flex flex-wrap gap-x-5 gap-y-2 border-b border-border"
        role="group"
        aria-label="Filter by beat"
      >
        {tab("all", "All stories", stories.length)}
        {present.map((b) =>
          tab(b, BEAT_LABEL[b], stories.filter((s) => s.beat === b).length),
        )}
      </div>
      {lead && (
        <div className="mt-8">
          <LeadStory story={lead} admin={admin} />
        </div>
      )}
      <div className="relative mt-8 grid gap-x-12 md:grid-cols-2 md:before:absolute md:before:inset-y-0 md:before:left-1/2 md:before:w-px md:before:bg-border">
        {rest.map((story) => (
          <StoryCard key={story.id} story={story} admin={admin} />
        ))}
      </div>
    </section>
  );
}
