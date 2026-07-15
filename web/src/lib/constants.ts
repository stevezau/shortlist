/** The row-size presets offered wherever a row length is chosen (settings, rows, wizard). */
export const ROW_SIZES = [10, 15, 20] as const;

/**
 * The seeded "Picked for You" row. Its name, size and curation style come from the global Settings
 * (the server drops its stored recipe — see ContextBuilder._build_rows), so the UI must neither
 * offer nor advertise per-row versions of those three on it. Its sources, libraries and audience
 * ARE its own, like any other row.
 */
export const DEFAULT_ROW_SLUG = "picked";

/** Display names for the curation tones (`PROMPT_TONES`), shared by the editor and the row list. */
export const TONE_LABELS: Record<string, string> = {
  balanced: "Balanced",
  warm: "Warm",
  concise: "Concise",
  cinephile: "Cinephile",
  playful: "Playful",
};

/**
 * The already-watched cap, as a whole percentage of the row that may be things the person has
 * already finished. 0 = all fresh (the default), 100 = no filtering. "Finished" means a movie
 * they've watched or a show they've seen most of — a partly-watched show or one with a new season
 * still counts as fresh. Stored as a 0..1 fraction; the UI works in whole percent.
 */
export const WATCHED_PCT_DEFAULT = 0;

/** Human sentence describing a given whole-percent cap, for helper text under the control. */
export function watchedPctDescription(pct: number): string {
  if (pct <= 0) return "Only fresh picks — nothing they’ve already finished.";
  if (pct >= 100)
    return "No filtering — already-watched titles can fill the whole row.";
  return `Up to ${pct}% of the row may be things they’ve already finished; the rest stays fresh.`;
}

/** Terse label for a row card's "this row overrides the watched cap" badge (fraction → percent). */
export function watchedBadgeLabel(pct: number): string {
  const whole = Math.round(pct * 100);
  if (whole <= 0) return "Watched: all fresh";
  if (whole >= 100) return "Watched: no filter";
  return `Watched: ≤${whole}%`;
}
