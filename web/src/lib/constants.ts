/**
 * Row length (title count) bounds, shared by every place a row size is chosen (settings, rows,
 * wizard, per-user override). A free number in this range — the server enforces the same bounds
 * (`row.size` validator, `CollectionIn.size`, `UserRowOverride.row_size`). The ceiling matches the
 * engine's fixed per-media pre-rank cap (`EngineConfig.candidates_pre_rank`, 40), so even a
 * single-media row at the max can actually be filled rather than silently truncated.
 */
export const ROW_SIZE_MIN = 5;
export const ROW_SIZE_MAX = 40;
export const ROW_SIZE_DEFAULT = 15;

/** Clamp any typed row size into the allowed range and to a whole number. */
export function clampRowSize(value: number): number {
  if (!Number.isFinite(value)) return ROW_SIZE_DEFAULT;
  return Math.max(ROW_SIZE_MIN, Math.min(ROW_SIZE_MAX, Math.round(value)));
}

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

/**
 * Freshness, as a whole percentage. 0 = stable (the same strong picks each day, best quality),
 * 100 = maximum variety (rotates the row daily and reaches deep down the ranked list for new
 * titles). In between trades quality for novelty. Stored as a 0..1 fraction; the UI is whole percent.
 */
export const FRESHNESS_DEFAULT = 0;

/** Human sentence describing a given whole-percent freshness, for helper text under the control. */
export function freshnessDescription(pct: number): string {
  if (pct <= 0)
    return "Stable — the same strong picks each day. Best match quality, least day-to-day change.";
  if (pct >= 100)
    return "Fresh — rotates the row every day and reaches deep for new titles. Most variety, occasionally weaker matches.";
  return `Rotates the row daily and reaches about ${pct}% down the ranked list for variety — higher is fresher, lower keeps the safest picks.`;
}

/** Terse label for a row card's "this row overrides the freshness" badge (fraction → percent). */
export function freshnessBadgeLabel(pct: number): string {
  const whole = Math.round(pct * 100);
  if (whole <= 0) return "Freshness: stable";
  if (whole >= 100) return "Freshness: max";
  return `Freshness: ${whole}%`;
}
