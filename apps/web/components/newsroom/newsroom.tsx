"use client";

import "@xyflow/react/dist/style.css";

import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import {
  Background,
  type Edge,
  Handle,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";
import { useEffect, useMemo, useRef, useState } from "react";

import { BEAT_DOT } from "@/components/beats";
import { useAdmin } from "@/hooks/use-admin";
import { useDarkMode } from "@/hooks/use-theme";
import {
  buildGraph,
  type GraphNode,
  isActive,
  type NodeState,
} from "@/lib/newsroom-graph";
import type { Run, RunEvent } from "@/lib/types";

import { ReviewPanel } from "./review-panel";
import { StartRun } from "./start-run";

const PUBLIC_API = (
  process.env.NEXT_PUBLIC_API_URL ?? "https://context-window-snrd.vercel.app"
).replace(/\/$/, "");

// Public reads go straight to the API; an admin's go through the server-side proxy,
// which adds the admin token and returns the full events.
function url(admin: boolean, path: string): string {
  return admin ? `/api/admin/${path}` : `${PUBLIC_API}/${path}`;
}

async function getJson<T>(href: string): Promise<T> {
  const response = await fetch(href, { cache: "no-store" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

const STATE_STYLE: Record<NodeState, string> = {
  waiting: "border-border text-muted-foreground",
  working:
    "border-amber-500 text-foreground shadow-[0_0_0_3px] shadow-amber-500/20",
  done: "border-emerald-600/60 text-foreground",
  failed: "border-red-600 text-foreground",
  paused: "border-sky-500 text-foreground shadow-[0_0_0_3px] shadow-sky-500/20",
};

const STATE_TEXT: Record<NodeState, string> = {
  waiting: "waiting",
  working: "working",
  done: "done",
  failed: "failed",
  paused: "paused",
};

function AgentNode({
  data,
}: NodeProps<Node<{ item: GraphNode; selected: boolean }>>) {
  const { item, selected } = data;
  return (
    <div
      className={`w-[150px] rounded-lg border bg-background px-3 py-2 text-left ${STATE_STYLE[item.state]} ${selected ? "ring-2 ring-ring" : ""}`}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!size-1.5 !border-0 !bg-border"
      />
      <p className="flex items-center gap-1.5 text-xs font-medium">
        {item.beat && (
          <span
            className={`size-2 rounded-full ${BEAT_DOT[item.beat]}`}
            aria-hidden
          />
        )}
        <span className="truncate">{item.label}</span>
      </p>
      <p className="mt-0.5 truncate text-[11px] text-muted-foreground">
        {item.detail || STATE_TEXT[item.state]}
      </p>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!size-1.5 !border-0 !bg-border"
      />
    </div>
  );
}

const nodeTypes = { agent: AgentNode };
const NODE_WIDTH = 150;
const GAP_X = 22;
const ROW_HEIGHT = 92;
const PER_ROW = 5;

function layout(items: GraphNode[], selected: string | null) {
  const byStage = new Map<number, GraphNode[]>();
  for (const item of items)
    byStage.set(item.stage, [...(byStage.get(item.stage) ?? []), item]);
  const nodes: Node<{ item: GraphNode; selected: boolean }>[] = [];
  let y = 0;
  for (const stage of [...byStage.keys()].sort((a, b) => a - b)) {
    const row = byStage.get(stage) ?? [];
    for (let i = 0; i < row.length; i += PER_ROW) {
      const chunk = row.slice(i, i + PER_ROW);
      const width = chunk.length * NODE_WIDTH + (chunk.length - 1) * GAP_X;
      chunk.forEach((item, j) => {
        nodes.push({
          id: item.id,
          type: "agent",
          position: { x: -width / 2 + j * (NODE_WIDTH + GAP_X), y },
          data: { item, selected: item.id === selected },
          draggable: false,
        });
      });
      y += ROW_HEIGHT;
    }
  }
  return nodes;
}

function NewsroomView() {
  const admin = useAdmin();
  const dark = useDarkMode();
  const [chosen, setChosen] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const runs = useQuery({
    queryKey: ["runs", admin],
    queryFn: () => getJson<Run[]>(url(admin, "runs?limit=8")),
    refetchInterval: (query) =>
      isActive(query.state.data?.[0]?.status) ? 2000 : 15000,
  });
  const run = runs.data?.find((r) => r.id === chosen) ?? runs.data?.[0];

  // Events arrive incrementally: keep what we have and ask only for newer ones, every 2 s
  // while the run is working and every 20 s otherwise.
  // Kept per run, so switching runs starts from an empty list without resetting state.
  const [fetched, setFetched] = useState<{ key: string; events: RunEvent[] }>({
    key: "",
    events: [],
  });
  const fast = useRef(false);
  useEffect(() => {
    fast.current = isActive(run?.status);
  }, [run?.status]);
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
          url(admin, `runs/${runId}/events?after=${after}`),
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
      if (!stopped) timer = setTimeout(tick, fast.current ? 2000 : 20000);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [runId, admin, key]);
  const all = useMemo(
    () => (fetched.key === key ? fetched.events : []),
    [fetched, key],
  );

  const graph = useMemo(() => buildGraph(all, run?.status), [all, run?.status]);
  const nodes = useMemo(
    () => layout(graph.nodes, selected),
    [graph.nodes, selected],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => ({
        ...e,
        animated:
          graph.nodes.find((n) => n.id === e.target)?.state === "working",
        style: { stroke: "var(--border)" },
      })),
    [graph],
  );
  const picked = graph.nodes.find((n) => n.id === selected) ?? null;
  const detail = useMemo(() => {
    if (!picked || !admin) return null;
    const match = [...all]
      .reverse()
      .find(
        (e) =>
          e.kind === "node.detail" &&
          (picked.branch
            ? e.payload.branch === picked.branch
            : e.payload.node === picked.node),
      );
    return match?.payload ?? null;
  }, [picked, admin, all]);

  return (
    <main className="mx-auto w-full max-w-[1100px] px-4 py-8 sm:px-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-heading text-3xl sm:text-4xl">Newsroom</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {run
              ? `Run for ${run.run_date}: ${run.status.replaceAll("_", " ")}`
              : runs.isLoading
                ? "Loading…"
                : "No runs yet."}
            {run && isActive(run.status) && " · live"}
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

      <div className="mt-6 grid gap-4 lg:grid-cols-[1fr_320px]">
        <div className="h-[560px] rounded-lg border border-border sm:h-[680px]">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.15 }}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
            colorMode={dark ? "dark" : "light"}
            onNodeClick={(_, node) => setSelected(node.id)}
            onPaneClick={() => setSelected(null)}
          >
            <Background gap={24} size={1} />
          </ReactFlow>
        </div>
        <aside className="space-y-4 text-sm">
          {admin && run?.status === "awaiting_review" ? (
            <ReviewPanel
              run={run}
              events={all}
              onDecided={() => void runs.refetch()}
            />
          ) : picked ? (
            <section className="rounded-lg border border-border p-4">
              <p className="font-medium">{picked.label}</p>
              <p className="mt-1 text-muted-foreground">
                {picked.detail || STATE_TEXT[picked.state]}
              </p>
              {admin ? (
                <pre className="mt-3 max-h-[480px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-[11px] leading-snug">
                  {detail
                    ? JSON.stringify(detail, null, 1).slice(0, 20000)
                    : "No output yet."}
                </pre>
              ) : (
                <p className="mt-3 text-xs text-muted-foreground">
                  Agent output is visible to the editor only.
                </p>
              )}
            </section>
          ) : (
            <section className="rounded-lg border border-border p-4 text-muted-foreground">
              <p>
                Each box is an agent. The editor plans, four reporters work in
                parallel, a fact-checker checks every story against its sources,
                then the writer drafts the briefing for review.
              </p>
              <p className="mt-2">Select a box to see more.</p>
              {run?.status === "awaiting_review" && !admin && (
                <p className="mt-2">
                  This run is waiting for the editor&apos;s review.
                </p>
              )}
            </section>
          )}
        </aside>
      </div>
    </main>
  );
}

export function Newsroom() {
  const [client] = useState(() => new QueryClient());
  return (
    <QueryClientProvider client={client}>
      <NewsroomView />
    </QueryClientProvider>
  );
}
