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
 * already watched. 0 = all fresh (the default), 100 = no filtering. Stored as a 0..1 fraction; the
 * UI works in whole percent.
 *
 * What "already watched" means depends on where the slider sits, and the two are worth keeping
 * straight because the difference was a real bug:
 *
 *   - at 0 it means TOUCHED — a movie they've seen, or a show they've watched any of. This matches
 *     Plex's own answer, which marks a show watched from its first episode.
 *   - above 0 it is a ceiling on FINISHED titles, where a show counts once they're most of the way
 *     through it. The cap needs a definite line for "N% of the row" to mean anything.
 */
export const WATCHED_PCT_DEFAULT = 0;

/** Human sentence describing a given whole-percent cap, for helper text under the control. */
export function watchedPctDescription(pct: number): string {
  if (pct <= 0)
    return "Only fresh picks — nothing they’ve watched, including shows they’ve only started.";
  if (pct >= 100)
    return "No filtering — already-watched titles can fill the whole row.";
  return `Up to ${pct}% of the row may be things they’ve finished; the rest stays fresh. Shows they’ve only started still count as fresh here.`;
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

/**
 * "Recent releases" — how much a title's RELEASE DATE counts when ranking it, as a whole
 * percentage. 0 = ignore age (how ranking worked before this existed), 100 = every
 * RECENCY_HALF_LIFE_YEARS of age halves a title's weight. Stored as a 0..1 fraction; UI is percent.
 *
 * Not freshness, despite living beside it in the same card. Freshness is a CADENCE — how often a
 * row re-picks. This is a PREFERENCE — which titles win when it does. A row can rebuild nightly and
 * still fill with 1990s titles; that combination is what this setting is for.
 *
 * Defaults to 50 to match the server (`recommendations.recency`) — the seed used only until settings
 * load. Every install gets this, existing servers included — no migration pins an older value,
 * so an upgraded server ranks exactly like a fresh one unless someone moves the slider.
 */
export const RECENCY_DEFAULT = 50;

/**
 * Years of age that halve a title's weight at full strength.
 *
 * MUST equal `RECENCY_HALF_LIFE_YEARS` in shortlist/engine/ranking.py. The strip under the slider
 * tells the owner what trade the setting is making, so a drift here means the UI advertises ranking
 * the engine is not doing. `recency.test.ts` pins both curves to the engine's own numbers.
 */
export const RECENCY_HALF_LIFE_YEARS = 8;

/** Ages, in years, that the era strip under the slider samples. */
const RECENCY_ERA_AGES = [0, 10, 20, 30, 40];

/**
 * How strongly a title of a given age ranks, 0..1 — the engine's `recency_factor`, in the browser.
 *
 * Age is clamped at 0 for the same reason the engine clamps it: a negative age (an unreleased
 * title) would raise `0.5` to a negative power and produce a weight above 1.
 */
export function recencyWeight(ageYears: number, pct: number): number {
  const strength = Math.min(100, Math.max(0, pct)) / 100;
  if (strength <= 0) return 1;
  const age = Math.max(0, ageYears);
  return Math.pow(0.5, (age / RECENCY_HALF_LIFE_YEARS) * strength);
}

/**
 * The era strip: what each vintage is worth at this setting, for the bars under the slider.
 *
 * Years are counted back from `currentYear` rather than written down, so the labels roll forward on
 * their own — the same reason the engine derives its year from the run instead of a constant.
 */
export function recencyEras(
  pct: number,
  currentYear: number,
): { year: number; weight: number }[] {
  return RECENCY_ERA_AGES.map((age) => ({
    year: currentYear - age,
    weight: recencyWeight(age, pct),
  }));
}

/**
 * Human sentence for the helper text under the control — a real year the reader can judge against
 * their own library, not an abstract multiplier.
 */
export function recencyDescription(pct: number, currentYear: number): string {
  if (pct <= 0)
    return "Off — release date is ignored. A great 1994 film ranks like a great one from this year.";
  // The age at which a title is worth about half a brand-new one: half-life / strength.
  const halfAge = Math.round(
    RECENCY_HALF_LIFE_YEARS / (Math.min(100, pct) / 100),
  );
  // Below ~20% that age falls outside the era strip, and clamping it to 40 used to make this
  // sentence claim "1986 ranks about half" while the bar directly above it read 84%. When the
  // half-point is off the end of the strip, state the weight the strip actually shows instead.
  const oldestAge = Math.max(...RECENCY_ERA_AGES);
  if (halfAge > oldestAge) {
    const weight = Math.round(recencyWeight(oldestAge, pct) * 100);
    return `Barely on — even a ${currentYear - oldestAge} title still ranks at ${weight}% of one from this year. Nudge it further right for a clearer lean towards new releases.`;
  }
  const halfYear = currentYear - halfAge;
  return `A ${halfYear} title ranks about half as strongly as one from this year. Older titles still reach rows — they just have to be a better match to earn it.`;
}

/** Terse label for a row card's "this row overrides the release-date weight" badge. */
export function recencyBadgeLabel(pct: number): string {
  const whole = Math.round(pct * 100);
  if (whole <= 0) return "Recent: any era";
  if (whole >= 100) return "Recent: strongly new";
  return `Recent: ${whole}%`;
}
