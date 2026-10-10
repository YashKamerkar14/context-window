"use client";

import { useState } from "react";

// Starts today's newsroom run through the admin proxy. By default the run pauses for
// review; if today's briefing is already published, it offers to run again anyway.
export function StartRun({ onStarted }: { onStarted: () => void }) {
  const [review, setReview] = useState(true);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [canForce, setCanForce] = useState(false);

  async function start(force: boolean) {
    setBusy(true);
    setNote("");
    try {
      const response = await fetch("/api/admin/runs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ force, review: review ? "manual" : "auto" }),
      });
      const body = (await response.json()) as {
        detail?: string | { detail?: string };
      };
      if (response.status === 202) {
        setNote("Started. GitHub needs about a minute before the run appears.");
        setCanForce(false);
        onStarted();
      } else {
        const detail =
          typeof body.detail === "string" ? body.detail : body.detail?.detail;
        setNote(detail ?? `HTTP ${response.status}`);
        setCanForce(
          response.status === 409 && Boolean(detail?.includes("published")),
        );
      }
    } catch (error) {
      setNote((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <label className="flex items-center gap-1.5 text-muted-foreground">
        <input
          type="checkbox"
          checked={review}
          onChange={(e) => setReview(e.target.checked)}
        />
        Pause for my review
      </label>
      <button
        type="button"
        disabled={busy}
        onClick={() => start(false)}
        className="rounded-md bg-primary px-3 py-1.5 text-primary-foreground hover:opacity-90 disabled:opacity-50"
      >
        Start a run
      </button>
      {canForce && (
        <button
          type="button"
          disabled={busy}
          onClick={() => start(true)}
          className="rounded-md border border-border px-3 py-1.5 hover:bg-muted"
        >
          Run again anyway
        </button>
      )}
      {note && (
        <p className="basis-full text-xs text-muted-foreground">{note}</p>
      )}
    </div>
  );
}
