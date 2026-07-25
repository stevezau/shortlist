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
 * The seeded "Picked for You" row. Its name and size come from the global Settings, so the UI must
 * neither offer nor advertise per-row versions of those two on it. Its sources, libraries and
 * audience ARE its own, like any other row.
 */
export const DEFAULT_ROW_SLUG = "picked";

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
 * Freshness, as a whole percentage — the REFRESH CADENCE (how often a row rebuilds), not a nightly
 * shuffle. 0 = never refresh once built (frozen/pinned), 100 = rebuild every night, in between =
 * every N days (50 ≈ weekly). On a refresh the strongest ~two-thirds stay and the weakest third is
 * swapped for new picks; other nights the row is reused unchanged (no re-curation, no Plex write).
 * Default weekly so rows stay stable and evolve gradually. Stored as a 0..1 fraction; UI is percent.
 */
export const FRESHNESS_DEFAULT = 50;

/** Roughly how many days between refreshes at a given whole-percent freshness (mirrors the engine's
 *  `_refresh_period_days`: 100 → nightly, lower → longer, capped near a fortnight). */
function refreshEveryDays(pct: number): number {
  const f = Math.min(100, Math.max(0, pct)) / 100;
  if (f >= 1) return 1;
  return Math.max(1, Math.round(1 + (1 - f) * 13));
}

/** Human sentence describing a given whole-percent freshness, for helper text under the control. */
export function freshnessDescription(pct: number): string {
  if (pct <= 0)
    return "Frozen — once built, the row never changes on its own. Pin a shelf you want to stay put.";
  if (pct >= 100)
    return "Rebuilds every night — the strongest two-thirds stay, the rest are swapped for new picks. Most variety, most Plex writes.";
  const days = refreshEveryDays(pct);
  const every =
    days <= 1
      ? "every night"
      : days >= 7
        ? `about every ${days} days`
        : `every ${days} days`;
  return `Refreshes ${every}: keeps the strongest two-thirds and swaps the rest for new picks. Other nights the row stays exactly as it is. Higher = fresher; lower = stickier.`;
}

/** Terse label for a row card's "this row overrides the freshness" badge (fraction → percent). */
export function freshnessBadgeLabel(pct: number): string {
  const whole = Math.round(pct * 100);
  if (whole <= 0) return "Freshness: frozen";
  if (whole >= 100) return "Freshness: nightly";
  return `Freshness: ${whole}%`;
}
