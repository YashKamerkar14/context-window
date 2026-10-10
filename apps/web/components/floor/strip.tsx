import Link from "next/link";

import { AGENTS } from "@/lib/floor";

/** "Made by 11 AI agents and one human": the team's avatars, linking to the floor. */
export function NewsroomStrip() {
  const team = Object.values(AGENTS);
  return (
    <Link
      href="/newsroom"
      className="group flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
    >
      <span className="flex -space-x-1.5" aria-hidden>
        {team.map((agent, i) => (
          <span
            key={agent.id}
            className={`cw-bob grid size-6 place-items-center rounded-full bg-background text-xs ring-1 ${agent.tone}`}
            style={{ animationDelay: `${i * 0.13}s` }}
          >
            {agent.avatar}
          </span>
        ))}
      </span>
      <span>
        Made by {team.length - 1} AI agents and one human{" "}
        <span className="inline-block transition-transform group-hover:translate-x-0.5">
          →
        </span>
      </span>
    </Link>
  );
}
