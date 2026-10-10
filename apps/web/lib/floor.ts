// The newsroom floor: who the agents are, and what each is doing at a moment in a run.
// `floorAt(events, clock)` replays a run's events up to `clock` (milliseconds since the
// epoch) into each agent's state, a wire of readable messages, and running counters.
// Live runs use "now" as the clock; replays move the clock forward faster than real time.
// Pure, so replay and live view share one code path.

import type { Beat, RunEvent } from "./types";

export type AgentId =
  | "editor"
  | "releases"
  | "research"
  | "industry"
  | "community"
  | "desk"
  | "factdesk"
  | "writer"
  | "copy"
  | "you"
  | "publisher"
  | "archive";

export type AgentState = "idle" | "working" | "done" | "waiting" | "failed";

export interface AgentInfo {
  id: AgentId;
  name: string;
  role: string;
  avatar: string;
  /** Tailwind classes for the avatar's ring and wire chip. */
  tone: string;
}

export const AGENTS: Record<AgentId, AgentInfo> = {
  editor: {
    id: "editor",
    name: "Maya",
    role: "Editor-in-chief",
    avatar: "🧑‍💼",
    tone: "bg-rose-500/15 ring-rose-500/50 text-rose-700 dark:text-rose-300",
  },
  releases: {
    id: "releases",
    name: "Leo",
    role: "Releases reporter",
    avatar: "🧑‍💻",
    tone: "bg-sky-500/15 ring-sky-500/50 text-sky-700 dark:text-sky-300",
  },
  research: {
    id: "research",
    name: "Iris",
    role: "Research reporter",
    avatar: "🔬",
    tone: "bg-violet-500/15 ring-violet-500/50 text-violet-700 dark:text-violet-300",
  },
  industry: {
    id: "industry",
    name: "Omar",
    role: "Industry reporter",
    avatar: "🏢",
    tone: "bg-amber-500/15 ring-amber-500/50 text-amber-700 dark:text-amber-300",
  },
  community: {
    id: "community",
    name: "Kai",
    role: "Community reporter",
    avatar: "💬",
    tone: "bg-emerald-500/15 ring-emerald-500/50 text-emerald-700 dark:text-emerald-300",
  },
  desk: {
    id: "desk",
    name: "Noor",
    role: "Desk editor",
    avatar: "🗂️",
    tone: "bg-orange-500/15 ring-orange-500/50 text-orange-700 dark:text-orange-300",
  },
  factdesk: {
    id: "factdesk",
    name: "Vera",
    role: "Fact desk",
    avatar: "🧐",
    tone: "bg-teal-500/15 ring-teal-500/50 text-teal-700 dark:text-teal-300",
  },
  writer: {
    id: "writer",
    name: "Sam",
    role: "Writer",
    avatar: "✍️",
    tone: "bg-indigo-500/15 ring-indigo-500/50 text-indigo-700 dark:text-indigo-300",
  },
  copy: {
    id: "copy",
    name: "Quinn",
    role: "Copy check",
    avatar: "🔍",
    tone: "bg-cyan-500/15 ring-cyan-500/50 text-cyan-700 dark:text-cyan-300",
  },
  you: {
    id: "you",
    name: "You",
    role: "Human editor",
    avatar: "👤",
    tone: "bg-zinc-500/15 ring-zinc-500/50 text-zinc-700 dark:text-zinc-300",
  },
  publisher: {
    id: "publisher",
    name: "Pax",
    role: "Publisher",
    avatar: "📰",
    tone: "bg-lime-500/15 ring-lime-500/50 text-lime-700 dark:text-lime-300",
  },
  archive: {
    id: "archive",
    name: "Arlo",
    role: "Archivist",
    avatar: "🗄️",
    tone: "bg-stone-500/15 ring-stone-500/50 text-stone-700 dark:text-stone-300",
  },
};

const BEAT_SOURCES: Record<Beat, string> = {
  releases: "GitHub releases and PyPI for the watchlist",
  research: "Hugging Face daily papers and arXiv",
  industry: "the AI labs' blogs",
  community: "Hacker News",
};

export interface Tile {
  branch: string;
  beat?: Beat;
  state: "checking" | "verified" | "dropped" | "rewritten";
}

export interface WireItem {
  id: number;
  at: number;
  agent: AgentId | "system";
  text: string;
}

export interface Floor {
  agents: Record<AgentId, { state: AgentState; says: string }>;
  tiles: Tile[];
  wire: WireItem[];
  counters: {
    items: number;
    stories: number;
    claims: number;
    verified: number;
    dropped: number;
  };
  /** Agents handing work to the next desk right now, as "from->to". */
  handoffs: Set<string>;
  finished: boolean;
}

function n(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function plural(count: number, word: string, words = `${word}s`): string {
  return `${count} ${count === 1 ? word : words}`;
}

export function eventTime(event: RunEvent): number {
  return new Date(event.ts).getTime();
}

const IDLE_LINES: Partial<Record<AgentId, string>> = {
  you: "Reads the draft if the run asks for a review",
};

/** "an industry story", "a research story", or "a story" when the beat is unknown. */
function storyOn(beat: string | null | undefined): string {
  if (!beat) return "a story";
  return `${/^[aeiou]/.test(beat) ? "an" : "a"} ${beat} story`;
}

export function floorAt(events: RunEvent[], clock: number): Floor {
  const agents = Object.fromEntries(
    (Object.keys(AGENTS) as AgentId[]).map((id) => [
      id,
      {
        state: "idle" as AgentState,
        says: IDLE_LINES[id] ?? "Waiting for work",
      },
    ]),
  ) as Floor["agents"];
  const tiles = new Map<string, Tile>();
  const wire: WireItem[] = [];
  const counters = { items: 0, stories: 0, claims: 0, verified: 0, dropped: 0 };
  const handoffs = new Set<string>();
  const reporterBeat = new Map<string, Beat>();
  let finished = false;
  let latest = 0;

  const set = (
    id: AgentId,
    state: AgentState,
    says: string,
    log = true,
    at = 0,
  ) => {
    agents[id] = { state, says };
    if (log) wire.push({ id: wire.length, at, agent: id, text: says });
  };

  for (const event of events) {
    const at = eventTime(event);
    if (at > clock) break;
    latest = at;
    const p = event.payload;
    const branch = typeof p.branch === "string" ? p.branch : null;
    const beat = (typeof p.beat === "string" ? p.beat : undefined) as
      Beat | undefined;
    if (branch && beat && branch.startsWith("reporter:"))
      reporterBeat.set(branch, beat);
    const reporter = branch ? reporterBeat.get(branch) : undefined;

    switch (
      event.kind === "progress" ? event.node : `${event.kind}:${event.node}`
    ) {
      case "run:run.started":
        wire.push({
          id: wire.length,
          at,
          agent: "system",
          text: `The newsroom opens for ${String(p.run_date ?? "today")}`,
        });
        set(
          "editor",
          "working",
          "Reading the watchlist and your interests…",
          false,
        );
        break;
      case "run:run.resumed":
        wire.push({
          id: wire.length,
          at,
          agent: "system",
          text: "The run picks up where it left off",
        });
        break;
      case "node:plan_coverage":
        set(
          "editor",
          "done",
          `Gave ${plural(n(p.assignments), "reporter")} today's brief`,
          true,
          at,
        );
        handoffs.clear();
        for (const b of [
          "releases",
          "research",
          "industry",
          "community",
        ] as Beat[]) {
          handoffs.add(`editor->${b}`);
        }
        break;
      case "reporter.fetching":
        if (beat)
          set(beat, "working", `Reading ${BEAT_SOURCES[beat]}…`, true, at);
        break;
      case "reporter.fetched":
        if (beat) {
          counters.items += n(p.items);
          set(
            beat,
            "working",
            `Found ${plural(n(p.items), "item")}; writing up ${n(p.leads)}…`,
            true,
            at,
          );
        }
        break;
      case "node:write":
        if (reporter) {
          counters.stories += n(p.candidates);
          set(
            reporter,
            "done",
            `Filed ${plural(n(p.candidates), "story", "stories")}`,
            true,
            at,
          );
          handoffs.add(`${reporter}->desk`);
        }
        break;
      case "node:merge_and_rank":
        for (const b of [
          "releases",
          "research",
          "industry",
          "community",
        ] as Beat[]) {
          handoffs.delete(`${b}->desk`);
          handoffs.delete(`editor->${b}`);
        }
        set(
          "desk",
          "done",
          `Ranked the stories; sent the best ${n(p.stories)} to fact-check`,
          true,
          at,
        );
        set("factdesk", "working", "Opening the sources…", false);
        handoffs.add("desk->factdesk");
        break;
      case "fact_check.checking":
        if (branch) {
          tiles.set(branch, { branch, beat, state: "checking" });
          counters.claims += n(p.claims);
          const where =
            typeof p.source === "string" ? ` against ${p.source}` : "";
          set(
            "factdesk",
            "working",
            `Checking ${plural(n(p.claims), "claim")} from ${beat ?? "a"} story${where}`,
            true,
            at,
          );
        }
        break;
      case "node:rewrite":
        if (branch) {
          const tile = tiles.get(branch);
          if (tile) tile.state = "rewritten";
          set(
            "factdesk",
            "working",
            "A claim didn't hold up: rewriting the story",
            true,
            at,
          );
        }
        break;
      case "node:finish":
        if (branch) {
          const tile = tiles.get(branch) ?? {
            branch,
            state: "checking" as const,
          };
          const dropped = n(p.dropped) > 0;
          tile.state = dropped ? "dropped" : "verified";
          tiles.set(branch, tile);
          if (dropped) counters.dropped += 1;
          else counters.verified += 1;
          set(
            "factdesk",
            "working",
            dropped
              ? `Dropped ${storyOn(tile.beat)}: its claims didn't check out`
              : `Verified ${storyOn(tile.beat)} against its sources`,
            true,
            at,
          );
        }
        break;
      case "node:write_briefing":
        handoffs.delete("desk->factdesk");
        set(
          "factdesk",
          "done",
          `${counters.verified} verified, ${counters.dropped} dropped`,
          false,
        );
        set(
          "writer",
          "done",
          "Drafted the briefing from the verified stories",
          true,
          at,
        );
        handoffs.add("writer->copy");
        break;
      case "node:check_draft":
        handoffs.delete("writer->copy");
        set(
          "copy",
          "done",
          "Checked the intro says nothing the stories don't",
          true,
          at,
        );
        handoffs.add("copy->you");
        break;
      case "review:__interrupt__":
        set(
          "you",
          "waiting",
          "Your turn: approve, edit or reject the draft",
          true,
          at,
        );
        break;
      case "node:human_review":
        handoffs.delete("copy->you");
        set("you", "done", "Approved the draft", true, at);
        handoffs.add("you->publisher");
        break;
      case "node:publish":
        handoffs.delete("you->publisher");
        set("publisher", "done", "Published today's briefing", true, at);
        handoffs.add("publisher->archive");
        break;
      case "node:update_memory":
        handoffs.delete("publisher->archive");
        set(
          "archive",
          "done",
          "Filed the stories so they won't repeat tomorrow",
          true,
          at,
        );
        break;
      case "run:run.finished":
        finished = true;
        wire.push({
          id: wire.length,
          at,
          agent: "system",
          text: `Run finished: ${String(p.status ?? "")}`,
        });
        break;
      case "run:run.paused":
        wire.push({
          id: wire.length,
          at,
          agent: "system",
          text: "The run pauses for review",
        });
        break;
      case "run:run.failed":
        finished = true;
        wire.push({
          id: wire.length,
          at,
          agent: "system",
          text: "The run failed",
        });
        break;
      default:
        break;
    }
  }

  // Desks whose turn has come but who have not reported yet are shown working.
  const reportersDone = (
    ["releases", "research", "industry", "community"] as AgentId[]
  ).every((id) => agents[id].state === "done");
  if (!finished && latest > 0) {
    if (reportersDone && agents.desk.state === "idle")
      agents.desk = {
        state: "working",
        says: "Merging duplicates and ranking…",
      };
    if (agents.factdesk.state === "done" && agents.writer.state === "idle")
      agents.writer = { state: "working", says: "Drafting the briefing…" };
    if (agents.writer.state === "done" && agents.copy.state === "idle")
      agents.copy = { state: "working", says: "Checking the intro…" };
  }

  return {
    agents,
    tiles: [...tiles.values()],
    wire,
    counters,
    handoffs,
    finished,
  };
}

/** A replay timeline: real gaps longer than `maxGap` ms (a pause for review that lasted
 * hours) are squeezed, so a replay never stalls. Returns playback times per event. */
export function compressTimeline(events: RunEvent[], maxGap = 4000): number[] {
  const times: number[] = [];
  let virtual = 0;
  let previous: number | null = null;
  for (const event of events) {
    const at = eventTime(event);
    virtual +=
      previous === null ? 0 : Math.min(Math.max(at - previous, 0), maxGap);
    times.push(virtual);
    previous = at;
  }
  return times;
}
