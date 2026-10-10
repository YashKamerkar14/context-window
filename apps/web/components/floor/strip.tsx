import Link from "next/link";

import { AGENTS } from "@/lib/floor";

/** "Made by a newsroom of AI agents": the team's avatars, linking to the floor. */
export function NewsroomStrip() {
  const team = Object.values(AGENTS);
  return (
    <Link
      href="/newsroom"
      className="group mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-border bg-card/60 px-4 py-3 transition-colors hover:border-foreground/40"
    >
      <span className="flex -space-x-1" aria-hidden>
        {team.map((agent, i) => (
          <span
            key={agent.id}
            className={`cw-bob grid size-8 place-items-center rounded-full bg-background text-base ring-2 ${agent.tone}`}
            style={{ animationDelay: `${i * 0.13}s` }}
          >
            {agent.avatar}
          </span>
        ))}
      </span>
      <span className="text-sm">
        <span className="font-semibold">Made by a newsroom of AI agents</span>
        <span className="text-muted-foreground">
          {" "}
          and one human. Watch them work{" "}
          <span className="inline-block transition-transform group-hover:translate-x-0.5">
            →
          </span>
        </span>
      </span>
    </Link>
  );
}
