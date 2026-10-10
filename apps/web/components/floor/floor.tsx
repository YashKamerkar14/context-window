"use client";

import "@xyflow/react/dist/style.css";

import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import {
  type Edge,
  type Node,
  ConnectionMode,
  type NodeChange,
  ReactFlow,
} from "@xyflow/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { ReviewPanel } from "@/components/newsroom/review-panel";
import { StartRun } from "@/components/newsroom/start-run";
import { useAdmin } from "@/hooks/use-admin";
import { useDarkMode } from "@/hooks/use-theme";
import {
  AGENTS,
  type AgentId,
  compressTimeline,
  eventTime,
  floorAt,
  type WireItem,
} from "@/lib/floor";
import { BEATS, type Run, type RunEvent, type RunStatus } from "@/lib/types";

import {
  DeskCard,
  type DeskData,
  DeskNode,
  type FactDeskData,
  FactDeskNode,
  type PaperData,
  PaperEdge,
} from "./desks";

const PUBLIC_API = (
  process.env.NEXT_PUBLIC_API_URL ?? "https://context-window-snrd.vercel.app"
).replace(/\/$/, "");

const ACTIVE: RunStatus[] = ["queued", "running", "resume_requested"];
const SPEEDS = [1, 2, 4, 10];

function url(admin: boolean, path: string): string {
  return admin ? `/api/admin/${path}` : `${PUBLIC_API}/${path}`;
}

async function getJson<T>(href: string): Promise<T> {
  const response = await fetch(href, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

// Where each desk sits: a snake from the editor, through the reporters and the fact
// desk, back along the bottom row to the archive. About 1010 x 770, so the whole floor
// fits a laptop screen at close to full size.
const SEATS: Record<AgentId, { x: number; y: number }> = {
  editor: { x: 390, y: 0 },
  releases: { x: 0, y: 170 },
  research: { x: 260, y: 170 },
  industry: { x: 520, y: 170 },
  community: { x: 780, y: 170 },
  desk: { x: 0, y: 400 },
  factdesk: { x: 295, y: 380 },
  writer: { x: 780, y: 400 },
  archive: { x: 0, y: 640 },
  publisher: { x: 260, y: 640 },
  you: { x: 520, y: 640 },
  copy: { x: 780, y: 640 },
};

type Side = "top" | "right" | "bottom" | "left";

const ROUTES: [AgentId, AgentId, Side, Side][] = [
  ["editor", "releases", "bottom", "top"],
  ["editor", "research", "bottom", "top"],
  ["editor", "industry", "bottom", "top"],
  ["editor", "community", "bottom", "top"],
  ["releases", "desk", "bottom", "top"],
  ["research", "desk", "bottom", "top"],
  ["industry", "desk", "bottom", "top"],
  ["community", "desk", "bottom", "top"],
  ["desk", "factdesk", "right", "left"],
  ["factdesk", "writer", "right", "left"],
  ["writer", "copy", "bottom", "top"],
  ["copy", "you", "left", "right"],
  ["you", "publisher", "left", "right"],
  ["publisher", "archive", "left", "right"],
];

/** The order work moves through the floor (the phone layout lists desks in it). */
const PIPELINE: AgentId[] = [
  "editor",
  "releases",
  "research",
  "industry",
  "community",
  "desk",
  "factdesk",
  "writer",
  "copy",
  "you",
  "publisher",
  "archive",
];

// The graph nodes behind each desk, for the editor's view of an agent's full output.
const DETAIL_NODES: Partial<Record<AgentId, string>> = {
  editor: "plan_coverage",
  desk: "merge_and_rank",
  factdesk: "fact_check",
  writer: "write_briefing",
  copy: "check_draft",
  you: "human_review",
  publisher: "publish",
  archive: "update_memory",
};

/** The latest full (admin-only) output of an agent. */
function detailFor(agent: AgentId, events: RunEvent[]): string {
  const beat = BEATS.find((b) => b === agent);
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.public) continue;
    if (beat) {
      if (
        event.node === "reporter" &&
        JSON.stringify(event.payload).includes(`"${beat}"`)
      ) {
        return JSON.stringify(event.payload, null, 2);
      }
    } else if (event.node === DETAIL_NODES[agent]) {
      return JSON.stringify(event.payload, null, 2);
    }
  }
  return "Nothing yet";
}

const nodeTypes = { desk: DeskNode, factdesk: FactDeskNode };
const edgeTypes = { paper: PaperEdge };

/** A number that counts up to its value. */
function Counter({ value, label }: { value: number; label: string }) {
  const [shown, setShown] = useState(value);
  const current = useRef(value);
  useEffect(() => {
    const from = current.current;
    const start = performance.now();
    let frame = 0;
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / 600);
      const next = Math.round(from + (value - from) * (1 - (1 - t) ** 3));
      current.current = next;
      setShown(next);
      if (t < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return (
    <div className="rounded-xl border border-border bg-card/70 px-3 py-2">
      <p className="font-heading text-2xl tabular-nums">
        {shown.toLocaleString("en-GB")}
      </p>
      <p className="text-[11px] text-muted-foreground">{label}</p>
    </div>
  );
}

function Wire({ items }: { items: WireItem[] }) {
  const latest = [...items].reverse().slice(0, 60);
  return (
    <section className="rounded-2xl border border-border bg-card/60 p-3">
      <p className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <span
          className="size-1.5 rounded-full bg-red-500 cw-live"
          aria-hidden
        />{" "}
        The wire
      </p>
      <ol
        className="max-h-[520px] space-y-2 overflow-y-auto pr-1"
        aria-live="polite"
      >
        {latest.length === 0 && (
          <li className="text-sm text-muted-foreground">Quiet so far.</li>
        )}
        {latest.map((item) => {
          const agent = item.agent === "system" ? null : AGENTS[item.agent];
          return (
            <li
              key={`${item.id}-${item.at}`}
              className="cw-slide flex gap-2 text-[13px] leading-snug"
            >
              <span
                className={`grid size-7 shrink-0 place-items-center rounded-full text-sm ring-1 ${agent ? agent.tone : "bg-muted ring-border"}`}
                aria-hidden
              >
                {agent ? agent.avatar : "📣"}
              </span>
              <span>
                {agent && <span className="font-semibold">{agent.name} </span>}
                <span className={agent ? "" : "text-muted-foreground"}>
                  {item.text}
                </span>
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function FloorView() {
  const admin = useAdmin();
  const dark = useDarkMode();
  const [chosen, setChosen] = useState<string | null>(null);
  const [selected, setSelected] = useState<AgentId | null>(null);

  const runs = useQuery({
    queryKey: ["runs", admin],
    queryFn: () => getJson<Run[]>(url(admin, "runs?limit=8")),
    refetchInterval: (query) =>
      ACTIVE.includes(query.state.data?.[0]?.status as RunStatus)
        ? 2000
        : 15000,
  });
  const run = runs.data?.find((r) => r.id === chosen) ?? runs.data?.[0];
  const live = run !== undefined && ACTIVE.includes(run.status);

  // The run's events, fetched incrementally (every 2 s while live).
  const [fetched, setFetched] = useState<{ key: string; events: RunEvent[] }>({
    key: "",
    events: [],
  });
  const fast = useRef(false);
  useEffect(() => {
    fast.current = live;
  }, [live]);
  const runId = run?.id;
  const key = `${runId ?? ""}:${admin}`;
  useEffect(() => {
    if (!runId) return;
    let stopped = false;
    let after = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const fresh = await getJson<RunEvent[]>(
          url(admin, `runs/${runId}/events?after=${after}&limit=1000`),
        );
        if (!stopped && fresh.length) {
          after = fresh[fresh.length - 1].id;
          setFetched((previous) => ({
            key,
            events:
              previous.key === key ? [...previous.events, ...fresh] : fresh,
          }));
        }
      } catch {
        // a missed poll is retried on the next tick
      }
      if (!stopped) timer = setTimeout(tick, fast.current ? 2000 : 30000);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [runId, admin, key]);
  const events = useMemo(
    () => (fetched.key === key ? fetched.events : []),
    [fetched, key],
  );

  // Replay: the same events on a squeezed timeline, played by a clock we control.
  const timeline = useMemo(() => compressTimeline(events), [events]);
  const replayEvents = useMemo(
    () =>
      events.map((e, i) => ({ ...e, ts: new Date(timeline[i]).toISOString() })),
    [events, timeline],
  );
  const duration = timeline.length ? timeline[timeline.length - 1] + 1500 : 0;
  const [speed, setSpeed] = useState(2);
  const [playing, setPlaying] = useState(true);
  const [playhead, setPlayhead] = useState(0);
  // A different run starts its replay from the top.
  const [replayKey, setReplayKey] = useState(key);
  if (replayKey !== key) {
    setReplayKey(key);
    setPlayhead(0);
    setPlaying(true);
  }
  const ended = duration > 0 && playhead >= duration;
  const running = playing && !ended;
  useEffect(() => {
    if (live || !running) return;
    let frame = 0;
    let last = performance.now();
    const step = (now: number) => {
      const elapsed = now - last;
      last = now;
      setPlayhead((p) => Math.min(duration, p + elapsed * speed));
      frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [live, running, speed, duration]);

  // Live: the clock is now, refreshed twice a second (0 until the first tick, read as
  // "everything so far", since the time can't be read while the page is prerendered).
  const [now, setNow] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [live]);

  // The floor changes only when the clock passes another event, so the desks re-render
  // per event rather than per animation frame.
  const source = live ? events : replayEvents;
  const clock = live ? now || Infinity : playhead;
  let reached = 0;
  while (reached < source.length && eventTime(source[reached]) <= clock)
    reached++;
  const floor = useMemo(
    () => floorAt(source.slice(0, reached), Infinity),
    [source, reached],
  );

  // React Flow measures each desk once; keep those sizes on the nodes we hand it, or a
  // rebuilt node is hidden again until measured.
  const [sizes, setSizes] = useState<
    Record<string, { width: number; height: number }>
  >({});
  const onNodesChange = (changes: NodeChange[]) => {
    const measured = changes.filter(
      (c) => c.type === "dimensions" && c.dimensions,
    );
    if (measured.length === 0) return;
    setSizes((previous) => {
      const next = { ...previous };
      for (const change of measured) {
        if (change.type === "dimensions" && change.dimensions)
          next[change.id] = change.dimensions;
      }
      return next;
    });
  };

  const nodes: Node<DeskData | FactDeskData>[] = useMemo(
    () =>
      (Object.keys(SEATS) as AgentId[]).map((agent) => ({
        id: agent,
        type: agent === "factdesk" ? "factdesk" : "desk",
        position: SEATS[agent],
        measured: sizes[agent],
        draggable: false,
        data: {
          agent,
          state: floor.agents[agent].state,
          says: floor.agents[agent].says,
          selected: agent === selected,
          ...(agent === "factdesk" ? { tiles: floor.tiles } : {}),
        },
      })),
    [floor, selected, sizes],
  );
  const edges: Edge<PaperData>[] = useMemo(
    () =>
      ROUTES.map(([from, to, sourceHandle, targetHandle]) => ({
        id: `${from}->${to}`,
        source: from,
        target: to,
        sourceHandle,
        targetHandle,
        type: "paper",
        data: { active: floor.handoffs.has(`${from}->${to}`) },
      })),
    [floor],
  );

  const status = !run
    ? {
        text: runs.isLoading ? "Connecting…" : "No runs yet",
        dot: "bg-muted-foreground",
      }
    : live
      ? { text: "Live", dot: "bg-red-500 cw-live" }
      : run.status === "awaiting_review"
        ? { text: "Waiting for review", dot: "bg-sky-500 cw-live" }
        : {
            text: running
              ? `Replay · ${speed}×`
              : ended
                ? "Replay finished"
                : "Replay paused",
            dot: "bg-amber-500",
          };

  return (
    <main className="mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide">
            <span className={`size-2 rounded-full ${status.dot}`} aria-hidden />
            {status.text}
          </p>
          <h1 className="mt-1 font-heading text-3xl sm:text-4xl">
            The newsroom floor
          </h1>
          <p className="mt-1 max-w-[60ch] text-sm text-muted-foreground">
            {live
              ? "These agents are working on the next briefing right now."
              : run
                ? `How the briefing for ${run.run_date} was made, replayed from the real run.`
                : "Every morning a team of AI agents reports, checks and writes the briefing."}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {runs.data && runs.data.length > 1 && (
            <select
              value={run?.id ?? ""}
              onChange={(e) => {
                setChosen(e.target.value);
                setSelected(null);
              }}
              className="rounded-md border border-input bg-background px-2 py-1.5 text-sm"
              aria-label="Choose a run"
            >
              {runs.data.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.run_date} · {r.status.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          )}
          {admin && <StartRun onStarted={() => void runs.refetch()} />}
        </div>
      </div>

      <div className="mt-5 grid grid-cols-3 gap-2 sm:grid-cols-5">
        <Counter value={floor.counters.items} label="Items read" />
        <Counter value={floor.counters.stories} label="Stories filed" />
        <Counter value={floor.counters.claims} label="Claims checked" />
        <Counter value={floor.counters.verified} label="Stories verified" />
        <Counter value={floor.counters.dropped} label="Stories dropped" />
      </div>

      {!live && duration > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-border px-3 py-2 text-sm">
          <button
            type="button"
            onClick={() => {
              if (ended) {
                setPlayhead(0);
                setPlaying(true);
              } else setPlaying(!playing);
            }}
            className="rounded-md bg-primary px-3 py-1 text-primary-foreground"
          >
            {running ? "Pause" : ended ? "Replay again" : "Play"}
          </button>
          <input
            type="range"
            min={0}
            max={duration}
            value={playhead}
            onChange={(e) => {
              setPlayhead(Number(e.target.value));
              setPlaying(false);
            }}
            className="min-w-[160px] flex-1 accent-foreground"
            aria-label="Replay position"
          />
          <div className="flex gap-1" role="group" aria-label="Replay speed">
            {SPEEDS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setSpeed(s)}
                aria-pressed={speed === s}
                className={`rounded-md border px-2 py-0.5 text-xs ${speed === s ? "border-foreground" : "border-border text-muted-foreground"}`}
              >
                {s}×
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="mt-4 grid gap-4 2xl:grid-cols-[1fr_360px]">
        <div className="relative hidden h-[780px] overflow-hidden rounded-2xl border border-border bg-[radial-gradient(circle_at_1px_1px,var(--border)_1px,transparent_0)] [background-size:22px_22px] md:block">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            onNodesChange={onNodesChange}
            connectionMode={ConnectionMode.Loose}
            fitView
            fitViewOptions={{ padding: 0.04 }}
            minZoom={0.3}
            maxZoom={1.5}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
            colorMode={dark ? "dark" : "light"}
            onNodeClick={(_, node) => setSelected(node.id as AgentId)}
            onPaneClick={() => setSelected(null)}
          />
        </div>
        <ol className="grid gap-2 sm:grid-cols-2 md:hidden">
          {PIPELINE.map((agent) => (
            <li key={agent}>
              <button
                type="button"
                onClick={() => setSelected(selected === agent ? null : agent)}
                className="block w-full text-left"
              >
                <DeskCard
                  agent={agent}
                  state={floor.agents[agent].state}
                  says={floor.agents[agent].says}
                  selected={agent === selected}
                />
              </button>
            </li>
          ))}
        </ol>
        <aside className="grid content-start gap-4 lg:grid-cols-2 2xl:grid-cols-1">
          {admin && run?.status === "awaiting_review" && (
            <ReviewPanel
              run={run}
              events={events}
              onDecided={() => void runs.refetch()}
            />
          )}
          {selected && (
            <section className="cw-slide rounded-2xl border border-border p-4 text-sm">
              <p className="flex items-center justify-between gap-2 font-semibold">
                <span className="flex items-center gap-2">
                  <span className="text-xl" aria-hidden>
                    {AGENTS[selected].avatar}
                  </span>
                  {AGENTS[selected].name}, {AGENTS[selected].role.toLowerCase()}
                </span>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  className="text-xs font-normal text-muted-foreground hover:text-foreground"
                >
                  Close
                </button>
              </p>
              <p className="mt-2 text-muted-foreground">
                {floor.agents[selected].says}
              </p>
              {admin ? (
                <pre className="mt-3 max-h-[360px] overflow-auto rounded-lg bg-muted p-3 text-[11px] leading-relaxed">
                  {detailFor(selected, events)}
                </pre>
              ) : (
                <p className="mt-2 text-xs text-muted-foreground">
                  The editor can see each agent&apos;s full output.
                </p>
              )}
            </section>
          )}
          <Wire items={floor.wire} />
        </aside>
      </div>
    </main>
  );
}

export function Floor() {
  const [client] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={client}>
      <FloorView />
    </QueryClientProvider>
  );
}
