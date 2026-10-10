// Turns a run's events into the newsroom graph: the fixed stages, plus one node per
// parallel branch (four reporters, about fourteen fact-checks) found in the events.
// Pure, so it is easy to reason about; the page only draws what this returns.

import type { Beat, RunEvent, RunStatus } from "./types";

export type NodeState = "waiting" | "working" | "done" | "failed" | "paused";

export interface GraphNode {
  id: string;
  label: string;
  detail: string;
  state: NodeState;
  stage: number;
  beat?: Beat;
  /** The event branch, for picking this node's admin detail. */
  branch: string | null;
  node: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
}

const STAGES = [
  { node: "plan_coverage", label: "Editor-in-chief" },
  { node: "reporter", label: "Reporters" },
  { node: "merge_and_rank", label: "Merge and rank" },
  { node: "fact_check", label: "Fact-checkers" },
  { node: "write_briefing", label: "Writer" },
  { node: "check_draft", label: "Draft check" },
  { node: "human_review", label: "Review" },
  { node: "publish", label: "Publish" },
  { node: "update_memory", label: "Memory" },
] as const;

const ACTIVE: RunStatus[] = ["running", "resume_requested"];

function num(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

interface Branch {
  id: string;
  beat?: Beat;
  detail: string;
  done: boolean;
  failed: boolean;
  started: boolean;
}

export function buildGraph(events: RunEvent[], status: RunStatus | undefined) {
  const done = new Set<string>();
  const counts = new Map<string, Record<string, unknown>>();
  const reporters = new Map<string, Branch>();
  const checks = new Map<string, Branch>();
  let paused = false;

  const branchOf = (map: Map<string, Branch>, id: string): Branch => {
    let branch = map.get(id);
    if (!branch) {
      branch = { id, detail: "", done: false, failed: false, started: false };
      map.set(id, branch);
    }
    return branch;
  };

  for (const event of events) {
    const p = event.payload;
    const branch = typeof p.branch === "string" ? p.branch : null;
    if (event.kind === "review") paused = true;
    if (event.node === "run.resumed") paused = false;

    if (branch?.startsWith("reporter:")) {
      const r = branchOf(reporters, branch);
      r.started = true;
      if (typeof p.beat === "string") r.beat = p.beat as Beat;
      if (event.node === "reporter.fetched") {
        r.detail = `${num(p.items) ?? 0} items, ${plural(num(p.leads) ?? 0, "lead")}`;
      }
      if (event.kind === "node" && event.node === "write") {
        r.done = true;
        r.detail = `${r.detail ? `${r.detail} → ` : ""}${plural(num(p.candidates) ?? 0, "story")}`;
      }
      continue;
    }
    if (branch?.startsWith("fact_check:")) {
      const c = branchOf(checks, branch);
      c.started = true;
      if (typeof p.beat === "string") c.beat = p.beat as Beat;
      if (event.node === "fact_check.checking" && !c.detail) {
        c.detail = plural(num(p.claims) ?? 0, "claim");
      }
      if (event.kind === "node" && event.node === "rewrite")
        c.detail = "rewritten";
      if (event.kind === "node" && event.node === "finish") {
        c.done = true;
        c.failed = (num(p.dropped) ?? 0) > 0;
        c.detail = c.failed ? "dropped" : "verified";
      }
      continue;
    }
    if (event.kind === "node" && branch === null) {
      done.add(event.node);
      counts.set(event.node, p);
    }
  }

  const active = status !== undefined && ACTIVE.includes(status);
  const finished =
    status === "published" || status === "rejected" || status === "failed";
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];
  let firstPending = true;
  let previous: string[] = [];

  const stageState = (node: string, anyStarted: boolean): NodeState => {
    if (node === "human_review" && paused && !done.has(node)) return "paused";
    if (done.has(node)) return "done";
    if (status === "failed" && firstPending && anyStarted) return "failed";
    if (active && firstPending) return "working";
    return "waiting";
  };

  STAGES.forEach((stage, index) => {
    let ids: string[];
    if (stage.node === "reporter" || stage.node === "fact_check") {
      const map = stage.node === "reporter" ? reporters : checks;
      const branches = [...map.values()];
      const stageDone = branches.length > 0 && branches.every((b) => b.done);
      if (branches.length === 0) {
        const state = stageState(stage.node, false);
        nodes.push({
          id: stage.node,
          label: stage.label,
          detail: "",
          state: finished ? "done" : state,
          stage: index,
          branch: null,
          node: stage.node,
        });
        ids = [stage.node];
      } else {
        ids = branches.map((b) => b.id);
        for (const b of branches) {
          nodes.push({
            id: b.id,
            label:
              stage.node === "reporter"
                ? `Reporter${b.beat ? `: ${b.beat}` : ""}`
                : "Fact-check",
            detail: b.detail,
            state: b.done
              ? b.failed
                ? "failed"
                : "done"
              : active
                ? "working"
                : "waiting",
            stage: index,
            beat: b.beat,
            branch: b.id,
            node: stage.node,
          });
        }
      }
      if (stageDone) done.add(stage.node);
      if (!stageDone && firstPending && (active || branches.length > 0))
        firstPending = false;
    } else {
      const state = stageState(stage.node, index > 0);
      const p = counts.get(stage.node) ?? {};
      let detail = "";
      if (stage.node === "plan_coverage")
        detail = plural(num(p.assignments) ?? 0, "beat");
      if (stage.node === "merge_and_rank" && num(p.stories) !== undefined) {
        detail = `${plural(num(p.stories) ?? 0, "story")} to check`;
      }
      if (stage.node === "human_review" && paused)
        detail = "waiting for a decision";
      nodes.push({
        id: stage.node,
        label: stage.label,
        detail,
        state,
        stage: index,
        branch: null,
        node: stage.node,
      });
      if (state !== "done") firstPending = false;
      ids = [stage.node];
    }
    for (const from of previous) {
      for (const to of ids)
        edges.push({ id: `${from}->${to}`, source: from, target: to });
    }
    previous = ids;
  });

  return { nodes, edges };
}

export function isActive(status: RunStatus | undefined): boolean {
  return (
    status !== undefined && (ACTIVE.includes(status) || status === "queued")
  );
}
