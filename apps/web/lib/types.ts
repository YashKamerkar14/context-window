// The API's response shapes (apps/api/src/context_window/api). Kept by hand: small, and
// read-only on this side.

export type Beat = "releases" | "research" | "industry" | "community";
export type Verdict = "supported" | "unsupported" | "unclear";

export interface Claim {
  id: string;
  text: string;
  source_url: string;
  verdict: Verdict | null;
  evidence_url: string | null;
  evidence_quote: string | null;
  evidence_kind: "page" | "feed" | null;
}

export interface Story {
  id: string;
  beat: Beat;
  headline: string;
  summary: string;
  why_it_matters: string;
  claims: Claim[];
  source_urls: string[];
  published_at: string | null;
  is_breaking_change: boolean;
  score: number;
  merged_from: number;
  edited_by_editor: boolean;
}

export interface Briefing {
  date: string;
  intro: string;
  light_day: boolean;
  stories: Story[];
  stats: {
    items_scanned: number;
    stories_kept: number;
    claims_verified: number;
  };
  run_id: string;
  emailed: boolean;
  previous_date: string | null;
  next_date: string | null;
}

export interface BriefingSummary {
  date: string;
  intro: string;
  stories: number;
  light_day: boolean;
}

export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_review"
  | "resume_requested"
  | "published"
  | "rejected"
  | "expired"
  | "failed";

export interface Run {
  id: string;
  run_date: string;
  trigger: string;
  status: RunStatus;
  started_at: string;
  finished_at: string | null;
  heartbeat_at: string | null;
  review_requested_at: string | null;
  stats: Record<string, unknown>;
  trace_url: string | null;
  actions_run_url: string | null;
}

export interface RunEvent {
  id: number;
  ts: string;
  node: string;
  kind: string;
  public: boolean;
  payload: Record<string, unknown>;
}

export const BEATS: Beat[] = ["releases", "research", "industry", "community"];
