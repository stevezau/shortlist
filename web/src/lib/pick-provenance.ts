import type { Pick } from "@/lib/types";

/** What each candidate source is called in the UI. Ids come from the engine's `candidate_sources`. */
const SOURCE_LABELS: Record<string, string> = {
  tmdb_similar: "TMDB",
  tmdb_discover: "TMDB (your genres)",
  trakt: "Trakt",
  llm_library: "AI, from your library",
  llm_web: "AI web search",
  cold_start: "Popular on this server",
  tmdb_both: "TMDB (similar + your genres)",
};

/**
 * How confident the suggestion was. `affinity` is 0..1: how near the top of the suggesting source's
 * list the title sat, so 1.0 means "the closest match it had" and 0.4 means "it was mentioned".
 *
 * Sources with no ranking of their own (Trakt, the AI sources) always report 1.0 — that is "no
 * ranking information", not "perfect match", which is why nothing here claims a strength for them.
 */
export type MatchStrength = "close" | "related" | "loose";

export function matchStrength(affinity: number): MatchStrength {
  if (affinity >= 0.8) return "close";
  if (affinity >= 0.5) return "related";
  return "loose";
}

export const STRENGTH_LABELS: Record<MatchStrength, string> = {
  close: "close match",
  related: "related",
  loose: "loosely related",
};

export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
}

/**
 * The one-line provenance for a pick: which source suggested it, and — for ranked sources — how
 * strongly. Empty when the pick predates provenance being recorded (pre-0035 rows), so the UI can
 * say nothing rather than imply a match it never measured.
 */
export function provenanceLabel(pick: Pick): string {
  const sources = pick.sources ?? [];
  if (sources.length === 0) return "";
  // Both TMDB sources on one pick would read "TMDB (your genres) + TMDB", which looks like a bug.
  const both = sources.includes("tmdb_similar") && sources.includes("tmdb_discover");
  const shown = both ? ["tmdb_both", ...sources.filter((s) => !s.startsWith("tmdb_"))] : sources;
  const names = shown.map(sourceLabel).join(" + ");
  // ONLY tmdb_similar ranks its suggestions. tmdb_discover is "popular in genres you like" — it is
  // permanently 1.0, so matching it here would stamp "close match" on every discover pick forever.
  const ranked = sources.includes("tmdb_similar");
  if (!ranked || pick.affinity === undefined) return `suggested by ${names}`;
  return `suggested by ${names} · ${STRENGTH_LABELS[matchStrength(pick.affinity)]}`;
}
