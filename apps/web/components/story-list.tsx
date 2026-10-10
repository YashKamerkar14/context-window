"use client";

import { useMemo, useState } from "react";

import { useAdmin } from "@/hooks/use-admin";
import { BEATS, type Beat, type Story } from "@/lib/types";

import { BEAT_LABEL, BEAT_STYLE } from "./beats";

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

function StoryRow({ story, admin }: { story: Story; admin: boolean }) {
  const sources = checkedSources(story);
  return (
    <article className="border-t border-border py-6">
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs font-medium">
        <span className={BEAT_STYLE[story.beat]}>{BEAT_LABEL[story.beat]}</span>
        {story.is_breaking_change && (
          <span className="text-red-700 dark:text-red-400">
            Breaking change
          </span>
        )}
        {story.edited_by_editor && (
          <span className="text-muted-foreground">Edited by the editor</span>
        )}
      </p>
      <h2 className="mt-2 font-heading text-xl leading-snug sm:text-2xl">
        {story.headline}
      </h2>
      <p className="mt-2 leading-relaxed">{story.summary}</p>
      <p className="mt-2 leading-relaxed text-muted-foreground">
        <span className="font-medium text-foreground">Why it matters: </span>
        {story.why_it_matters}
      </p>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
        <details className="group text-sm">
          <summary className="cursor-pointer list-none text-muted-foreground hover:text-foreground">
            <span className="group-open:hidden">▸</span>
            <span className="hidden group-open:inline">▾</span> Checked against{" "}
            {sources.length} {sources.length === 1 ? "source" : "sources"}
          </summary>
          <ul className="mt-3 space-y-3 border-l border-border pl-4">
            {story.claims.map((claim) => (
              <li key={claim.id}>
                <p>{claim.text}</p>
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
        {admin && <Feedback storyId={story.id} />}
      </div>
    </article>
  );
}

export function StoryList({ stories }: { stories: Story[] }) {
  const admin = useAdmin();
  const [beat, setBeat] = useState<Beat | "all">("all");
  const present = useMemo(
    () => BEATS.filter((b) => stories.some((s) => s.beat === b)),
    [stories],
  );
  const shown =
    beat === "all" ? stories : stories.filter((s) => s.beat === beat);
  const pill = (value: Beat | "all", label: string) => (
    <button
      key={value}
      type="button"
      onClick={() => setBeat(value)}
      aria-pressed={beat === value}
      className={`rounded-full border px-3 py-1 text-sm ${
        beat === value
          ? "border-foreground text-foreground"
          : "border-border text-muted-foreground hover:text-foreground"
      }`}
    >
      {label}
    </button>
  );
  return (
    <section className="mt-8">
      <div
        className="flex flex-wrap gap-2"
        role="group"
        aria-label="Filter by beat"
      >
        {pill("all", "All")}
        {present.map((b) => pill(b, BEAT_LABEL[b]))}
      </div>
      <div className="mt-4">
        {shown.map((story) => (
          <StoryRow key={story.id} story={story} admin={admin} />
        ))}
      </div>
    </section>
  );
}
