import type { Beat } from "@/lib/types";

// Colour is used only for beat labels and status (PLAN.md 9).
export const BEAT_STYLE: Record<Beat, string> = {
  releases: "text-sky-700 dark:text-sky-300",
  research: "text-violet-700 dark:text-violet-300",
  industry: "text-amber-700 dark:text-amber-300",
  community: "text-emerald-700 dark:text-emerald-300",
};

export const BEAT_DOT: Record<Beat, string> = {
  releases: "bg-sky-500",
  research: "bg-violet-500",
  industry: "bg-amber-500",
  community: "bg-emerald-500",
};

export const BEAT_LABEL: Record<Beat, string> = {
  releases: "Releases",
  research: "Research",
  industry: "Industry",
  community: "Community",
};
