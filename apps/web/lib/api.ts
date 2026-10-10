// Server-side reads from the API. Briefings are cached and tagged "briefings"; publishing
// calls POST /api/revalidate, which expires the tag so the new briefing shows at once.
// Never import this file from a client component.

import { cacheLife, cacheTag } from "next/cache";

import type { Briefing, BriefingSummary, Run } from "./types";

export const API_URL = (
  process.env.API_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "https://context-window-snrd.vercel.app"
).replace(/\/$/, "");

async function get<T>(path: string): Promise<T | null> {
  const response = await fetch(`${API_URL}${path}`, {
    headers: { accept: "application/json" },
  });
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`API ${path}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

export async function latestBriefing(): Promise<Briefing | null> {
  "use cache";
  cacheLife("hours");
  cacheTag("briefings");
  return get<Briefing>("/briefings/latest");
}

export async function briefingFor(date: string): Promise<Briefing | null> {
  "use cache";
  cacheLife("days");
  cacheTag("briefings");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  return get<Briefing>(`/briefings/${date}`);
}

export async function archive(): Promise<BriefingSummary[]> {
  "use cache";
  cacheLife("hours");
  cacheTag("briefings");
  return (await get<BriefingSummary[]>("/briefings?limit=90")) ?? [];
}

export async function latestRun(): Promise<Run | null> {
  "use cache";
  cacheLife("minutes");
  cacheTag("briefings");
  const runs = await get<Run[]>("/runs?limit=1");
  return runs?.[0] ?? null;
}
