"use client";

import {
  BaseEdge,
  type Edge,
  type EdgeProps,
  getBezierPath,
  Handle,
  type Node,
  type NodeProps,
  Position,
} from "@xyflow/react";

import { AGENTS, type AgentId, type AgentState, type Tile } from "@/lib/floor";

export type DeskData = {
  agent: AgentId;
  state: AgentState;
  says: string;
  selected: boolean;
};

export type FactDeskData = DeskData & { tiles: Tile[] };

const STATE_BADGE: Record<AgentState, { text: string; className: string }> = {
  idle: { text: "idle", className: "text-muted-foreground" },
  working: { text: "working", className: "text-amber-600 dark:text-amber-400" },
  done: { text: "done", className: "text-emerald-600 dark:text-emerald-400" },
  waiting: { text: "your turn", className: "text-sky-600 dark:text-sky-400" },
  failed: { text: "failed", className: "text-red-600 dark:text-red-400" },
};

function Avatar({ agent, state }: { agent: AgentId; state: AgentState }) {
  const info = AGENTS[agent];
  const active = state === "working" || state === "waiting";
  return (
    <div className={`relative ${active ? "cw-bob" : ""}`}>
      <div
        className={`grid size-12 place-items-center rounded-full text-2xl ring-2 ${info.tone} ${active ? "cw-ring" : ""} ${state === "idle" ? "opacity-60 grayscale" : ""}`}
        aria-hidden
      >
        {info.avatar}
      </div>
      {state === "done" && (
        <span className="absolute -right-1 -bottom-1 grid size-5 place-items-center rounded-full bg-emerald-500 text-[11px] text-white">
          ✓
        </span>
      )}
      {state === "idle" && (
        <span className="absolute -top-2 -right-2 text-[10px] text-muted-foreground">
          z z
        </span>
      )}
    </div>
  );
}

function Bubble({ says, state }: { says: string; state: AgentState }) {
  return (
    <div
      className={`relative mt-3 rounded-xl border bg-background px-3 py-2 text-[12px] leading-snug shadow-sm ${
        state === "working" || state === "waiting"
          ? "border-foreground/30"
          : "border-border text-muted-foreground"
      }`}
    >
      <span className="absolute -top-1.5 left-6 size-3 rotate-45 border-t border-l border-inherit bg-background" />
      <span className="relative">
        {state === "working" ? says.replace(/…$/, "") : says}
      </span>
      {state === "working" && (
        <span className="cw-dots ml-1 text-muted-foreground" aria-hidden>
          <span>.</span>
          <span>.</span>
          <span>.</span>
        </span>
      )}
    </div>
  );
}

function DeskHeader({ agent, state }: { agent: AgentId; state: AgentState }) {
  const info = AGENTS[agent];
  const badge = STATE_BADGE[state];
  return (
    <div className="flex items-center gap-3">
      <Avatar agent={agent} state={state} />
      <div className="min-w-0">
        <p className="truncate text-sm font-semibold">{info.name}</p>
        <p className="truncate text-[11px] text-muted-foreground">
          {info.role}
        </p>
        <p
          className={`text-[10px] font-medium uppercase tracking-wide ${badge.className}`}
        >
          {badge.text}
        </p>
      </div>
    </div>
  );
}

const HANDLE_CLASS = "!size-1.5 !border-0 !bg-transparent";

/** A connection point on each side; routes pick sides by id (top, right, bottom, left). */
const handles = (
  <>
    {(
      [
        ["top", Position.Top],
        ["right", Position.Right],
        ["bottom", Position.Bottom],
        ["left", Position.Left],
      ] as const
    ).map(([id, position]) => (
      <Handle
        key={id}
        id={id}
        type="source"
        position={position}
        className={HANDLE_CLASS}
      />
    ))}
  </>
);

/** One agent's desk, outside the canvas too (the phone layout lists them as cards). */
export function DeskCard({
  agent,
  state,
  says,
  selected,
  className = "",
}: DeskData & { className?: string }) {
  return (
    <div
      className={`rounded-2xl border bg-card/90 p-3 backdrop-blur transition-colors ${
        selected
          ? "border-foreground"
          : state === "waiting"
            ? "border-sky-500"
            : "border-border"
      } ${className}`}
    >
      <DeskHeader agent={agent} state={state} />
      <Bubble says={says} state={state} />
    </div>
  );
}

export function DeskNode({ data }: NodeProps<Node<DeskData>>) {
  return (
    <>
      {handles}
      <DeskCard {...data} className="w-[230px]" />
    </>
  );
}

const TILE_STYLE: Record<Tile["state"], string> = {
  checking: "bg-amber-400/80 cw-live",
  rewritten: "bg-sky-400/80",
  verified: "bg-emerald-500 cw-flip",
  dropped: "bg-red-500 cw-flip",
};

export function FactDeskNode({ data }: NodeProps<Node<FactDeskData>>) {
  const verified = data.tiles.filter((t) => t.state === "verified").length;
  const dropped = data.tiles.filter((t) => t.state === "dropped").length;
  return (
    <div
      className={`w-[420px] rounded-2xl border bg-card/90 p-3 backdrop-blur ${data.selected ? "border-foreground" : "border-border"}`}
    >
      {handles}
      <div className="flex items-start justify-between gap-3">
        <DeskHeader agent="factdesk" state={data.state} />
        <p className="text-right text-[11px] text-muted-foreground">
          <span className="text-emerald-600 dark:text-emerald-400">
            {verified} verified
          </span>
          <br />
          <span className="text-red-600 dark:text-red-400">
            {dropped} dropped
          </span>
        </p>
      </div>
      <div
        className="mt-3 grid grid-cols-7 gap-1.5"
        aria-label="One tile per story being checked"
      >
        {data.tiles.length === 0
          ? Array.from({ length: 14 }, (_, i) => (
              <div
                key={i}
                className="h-7 rounded-md border border-dashed border-border"
              />
            ))
          : data.tiles.map((tile) => (
              <div
                key={`${tile.branch}-${tile.state}`}
                title={`${tile.beat ?? "story"}: ${tile.state}`}
                className={`grid h-7 place-items-center rounded-md text-[11px] text-white ${TILE_STYLE[tile.state]}`}
              >
                {tile.state === "verified"
                  ? "✓"
                  : tile.state === "dropped"
                    ? "✕"
                    : tile.state === "rewritten"
                      ? "✎"
                      : "…"}
              </div>
            ))}
      </div>
      <Bubble says={data.says} state={data.state} />
    </div>
  );
}

export type PaperData = { active: boolean };

/** A route between desks; while work is handed over, paper flies along it. */
export function PaperEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
}: EdgeProps<Edge<PaperData>>) {
  const [path] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });
  const active = Boolean(data?.active);
  return (
    <>
      <BaseEdge
        id={id}
        path={path}
        className={active ? "cw-dash" : ""}
        style={{
          stroke: active ? "var(--foreground)" : "var(--border)",
          strokeWidth: active ? 1.5 : 1,
          opacity: active ? 0.6 : 1,
        }}
      />
      {active && (
        <text
          className="cw-paper"
          fontSize={16}
          textAnchor="middle"
          dominantBaseline="middle"
          aria-hidden
        >
          📄
          <animateMotion dur="1.6s" repeatCount="indefinite" path={path} />
        </text>
      )}
    </>
  );
}
