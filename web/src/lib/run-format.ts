import type { RunLogEntry } from "@/lib/types";
import { isServerStage, progressLabel, STAGE_LABELS } from "@/lib/run-stages";

/** The one recognised failure class a raw engine/Plex error belongs to, or `null` for anything
 *  unrecognised. The single source of truth both `friendlyError` (what to SAY) and `errorBucket`
 *  (what counts as "the same problem") are built from, so the two can never drift apart. */
type ErrorClass = "plex_5xx" | "timeout" | "rate_limited" | null;

function classifyError(raw: string): ErrorClass {
  if (/\b50\d\b|internal_server_error/i.test(raw)) return "plex_5xx";
  if (/timed?\s?out|timeout/i.test(raw)) return "timeout";
  if (/\b429\b|too many requests/i.test(raw)) return "rate_limited";
  return null;
}

/** A one-line, plain-English take on a raw engine/Plex error — the raw text stays available below. */
export function friendlyError(raw: string): string {
  switch (classifyError(raw)) {
    case "plex_5xx":
      return "Plex hit a server error (500) while writing this row — it was most likely overloaded. This usually clears on the next run.";
    case "timeout":
      return "Plex timed out while writing this row — it was busy. This usually clears on the next run.";
    case "rate_limited":
      return "Plex was rate-limiting writes (429) — too many at once. This usually clears on the next run.";
    default:
      return "Something went wrong building this person’s row.";
  }
}

/**
 * A bucket key for "N people failed with the same problem" — deliberately NARROWER than
 * `friendlyError`. `friendlyError` returns one generic sentence for any unrecognised error, so
 * bucketing on ITS return value grouped five unrelated failures under a claim that they were the
 * same problem. Only the three recognised classes above are ever claimed to match one another; an
 * unrecognised error gets no bucket (`null`) and is never counted toward the banner.
 */
export function errorBucket(raw: string): ErrorClass {
  return classifyError(raw);
}

/** Rank badge colour by tier — the top picks stand out, lower ones recede. */
export function rankClass(rank: number): string {
  if (rank <= 3) return "text-amber-400";
  if (rank <= 10) return "text-foreground";
  return "text-muted-foreground";
}

/** Plain-English names for the AI steps in an llm_tokens_by_step map. */
const STEP_LABELS: Record<string, string> = {
  curate: "final picks",
  llm_web: "web search",
  llm_library: "library scan",
};

/** "final picks 12,340 · web search 4,100" for a by-step token map, or "" when empty. Callers wrap
 *  it in parentheses or not, whichever their sentence needs — the two previous copies differed only
 *  by that punctuation. */
export function tokenStepBreakdown(byStep?: Record<string, number>): string {
  if (!byStep) return "";
  return Object.entries(byStep)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([step, n]) => `${STEP_LABELS[step] ?? step} ${n.toLocaleString()}`)
    .join(" · ");
}

/** " · N Exa search(es)" when any ran, else "". Exa bills per search, so it's shown apart from tokens. */
export function exaSummary(count?: number): string {
  if (!count) return "";
  return ` · ${count} Exa search${count === 1 ? "" : "es"}`;
}

/** The stage the run is in RIGHT NOW, phrased for the header.
 *
 *  Everything after the last person finishes is server-wide, and used to be silent — so a run in its
 *  tail looked identical to a wedged one. Naming the phase is the whole fix. */
export function currentPhase(entries: RunLogEntry[]): string | null {
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const entry = entries[i];
    if (!entry || !isServerStage(entry.user)) continue;
    if (entry.stage === "finished") return null;
    const label = STAGE_LABELS[entry.stage] ?? entry.stage;
    const progress = progressLabel(entry.counts ?? {});
    return progress ? `${label} ${progress}` : label;
  }
  return null;
}
