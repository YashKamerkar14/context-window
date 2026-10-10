"use client";

import { useMemo, useState } from "react";

import { BEAT_LABEL, BEAT_STYLE } from "@/components/beats";
import type { Run, RunEvent, Story } from "@/lib/types";

interface Edit {
  headline: string;
  summary: string;
  why_it_matters: string;
}

/** The draft's full stories, from the latest admin detail of the writer or the draft check. */
function draftStories(events: RunEvent[]): Story[] {
  for (const event of [...events].reverse()) {
    if (event.kind !== "node.detail") continue;
    if (event.node !== "check_draft" && event.node !== "write_briefing")
      continue;
    const update = event.payload.update as
      { draft?: { stories?: Story[] } } | null | undefined;
    if (update?.draft?.stories) return update.draft.stories;
  }
  return [];
}

function edited(story: Story, edit: Edit): Story {
  const summary = edit.summary.trim();
  return {
    ...story,
    headline: edit.headline.trim(),
    summary,
    why_it_matters: edit.why_it_matters.trim(),
    // The new wording was not fact-checked: it is shown as "edited by the editor", and its
    // one sentence still cites every claim the story was checked on.
    ...({
      sentence_claim_map: [
        { sentence: summary, claim_ids: story.claims.map((c) => c.id) },
      ],
    } as object),
  };
}

export function ReviewPanel({
  run,
  events,
  onDecided,
}: {
  run: Run;
  events: RunEvent[];
  onDecided: () => void;
}) {
  const stories = useMemo(() => draftStories(events), [events]);
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">(
    "idle",
  );
  const [message, setMessage] = useState("");

  async function decide(action: "approve" | "reject") {
    if (
      action === "reject" &&
      !window.confirm("Reject this draft? Nothing will be published today.")
    ) {
      return;
    }
    const changed = Object.keys(edits).length > 0 || removed.size > 0;
    const body =
      action === "reject"
        ? { action: "reject" }
        : {
            action: changed ? "edit" : "approve",
            removed_story_ids: [...removed],
            edited_stories: stories
              .filter((s) => edits[s.id] && !removed.has(s.id))
              .map((s) => edited(s, edits[s.id])),
          };
    setState("sending");
    try {
      const response = await fetch(`/api/admin/runs/${run.id}/review`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = (await response.json()) as { detail?: unknown };
      if (!response.ok)
        throw new Error(JSON.stringify(result.detail ?? response.status));
      setState("sent");
      setMessage(
        action === "reject"
          ? "Rejected. The run will end without publishing."
          : "Approved. The run resumes on GitHub and publishes in about a minute.",
      );
      onDecided();
    } catch (error) {
      setState("error");
      setMessage(`That did not go through: ${(error as Error).message}`);
    }
  }

  if (state === "sent") {
    return (
      <section className="rounded-lg border border-border p-4">
        {message}
      </section>
    );
  }
  return (
    <section className="rounded-lg border border-sky-500/60 p-4">
      <p className="font-medium">Review the draft</p>
      <p className="mt-1 text-muted-foreground">
        Remove stories you do not want, or edit their wording, then approve.
      </p>
      {stories.length === 0 && (
        <p className="mt-3 text-muted-foreground">Loading the draft…</p>
      )}
      <ul className="mt-3 space-y-3">
        {stories.map((story) => {
          const gone = removed.has(story.id);
          const edit = edits[story.id];
          return (
            <li
              key={story.id}
              className={`border-t border-border pt-3 ${gone ? "opacity-50" : ""}`}
            >
              <p className={`text-xs ${BEAT_STYLE[story.beat]}`}>
                {BEAT_LABEL[story.beat]}
              </p>
              {editing === story.id ? (
                <div className="mt-1 space-y-2">
                  {(["headline", "summary", "why_it_matters"] as const).map(
                    (field) => (
                      <textarea
                        key={field}
                        aria-label={field.replaceAll("_", " ")}
                        value={(edit ?? story)[field]}
                        onChange={(e) =>
                          setEdits({
                            ...edits,
                            [story.id]: {
                              ...(edit ?? {
                                headline: story.headline,
                                summary: story.summary,
                                why_it_matters: story.why_it_matters,
                              }),
                              [field]: e.target.value,
                            },
                          })
                        }
                        rows={field === "headline" ? 2 : 3}
                        className="block w-full rounded-md border border-input bg-background p-2 text-xs"
                      />
                    ),
                  )}
                  <button
                    type="button"
                    onClick={() => setEditing(null)}
                    className="text-xs underline"
                  >
                    Done editing
                  </button>
                </div>
              ) : (
                <p className={`mt-0.5 ${gone ? "line-through" : ""}`}>
                  {(edit ?? story).headline}
                  {edit && (
                    <span className="ml-1 text-xs text-muted-foreground">
                      (edited)
                    </span>
                  )}
                </p>
              )}
              <div className="mt-1 flex gap-3 text-xs">
                <button
                  type="button"
                  onClick={() => {
                    const next = new Set(removed);
                    if (gone) next.delete(story.id);
                    else next.add(story.id);
                    setRemoved(next);
                  }}
                  className="text-muted-foreground underline hover:text-foreground"
                >
                  {gone ? "Keep" : "Remove"}
                </button>
                {!gone && editing !== story.id && (
                  <button
                    type="button"
                    onClick={() => setEditing(story.id)}
                    className="text-muted-foreground underline hover:text-foreground"
                  >
                    Edit
                  </button>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {state === "error" && (
        <p className="mt-3 text-red-700 dark:text-red-400">{message}</p>
      )}
      <div className="mt-4 flex gap-2">
        <button
          type="button"
          disabled={state === "sending" || stories.length === 0}
          onClick={() => decide("approve")}
          className="flex-1 rounded-md bg-primary px-3 py-2 text-primary-foreground hover:opacity-90 disabled:opacity-50"
        >
          {removed.size || Object.keys(edits).length
            ? "Approve with changes"
            : "Approve"}
        </button>
        <button
          type="button"
          disabled={state === "sending"}
          onClick={() => decide("reject")}
          className="rounded-md border border-border px-3 py-2 hover:bg-muted"
        >
          Reject
        </button>
      </div>
    </section>
  );
}
