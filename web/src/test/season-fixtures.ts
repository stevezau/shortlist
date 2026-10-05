import type { DateRule, Season, SeasonPreset, SeasonPreview } from "@/lib/types";

/**
 * Seasons as `GET /api/seasons` serves them (#137): built-ins carry no sources and follow the row's
 * timing (`lead_days`/`after_days` null); the owner's own carry both. Dates as on 2 Oct 2026.
 */

export function fixedRule(month: number, day: number): DateRule {
  return { kind: "fixed", month, day, nth: 1, weekday: 0, offset: 0 };
}

export function builtin(
  slug: string,
  name: string,
  emoji: string,
  rule: DateRule,
  ruleLabel: string,
  nextDates: string[],
  description = "",
): Season {
  return {
    slug,
    name,
    emoji,
    description,
    builtin: true,
    rule,
    rule_label: ruleLabel,
    next_dates: nextDates,
    lead_days: null,
    after_days: null,
    preset: null,
    tags: [],
    genre: null,
    excluded_genres: [],
    collections: [],
    picks: [],
    used_by: [],
  };
}

export const VALENTINES = builtin(
  "valentines",
  "Valentine's Day",
  "💘",
  fixedRule(2, 14),
  "14 February",
  ["2027-02-14", "2028-02-14"],
  "Valentine's films and romance",
);
export const HALLOWEEN = builtin(
  "halloween",
  "Halloween",
  "🎃",
  fixedRule(10, 31),
  "31 October",
  ["2026-10-31", "2027-10-31"],
  "Halloween films and horror",
);
export const CHRISTMAS = builtin(
  "christmas",
  "Christmas",
  "🎄",
  fixedRule(12, 25),
  "25 December",
  ["2026-12-25", "2027-12-25"],
  "Christmas films",
);

export const THANKSGIVING: Season = {
  slug: "thanksgiving",
  name: "Thanksgiving",
  emoji: "🦃",
  description: "",
  builtin: false,
  rule: { kind: "nth", month: 11, day: 1, nth: 4, weekday: 3, offset: 0 },
  rule_label: "4th Thursday of November",
  next_dates: ["2026-11-26", "2027-11-25"],
  lead_days: 14,
  after_days: 0,
  preset: "thanksgiving_us",
  tags: [{ id: 4543, name: "thanksgiving" }],
  genre: null,
  excluded_genres: [],
  collections: [],
  picks: [],
  used_by: [],
};

export const BUILTINS: Season[] = [VALENTINES, HALLOWEEN, CHRISTMAS];

/** Every built-in plus one of the owner's own, in calendar order. */
export const CATALOGUE: Season[] = [VALENTINES, HALLOWEEN, THANKSGIVING, CHRISTMAS];

export const THANKSGIVING_US: SeasonPreset = {
  key: "thanksgiving_us",
  label: "Thanksgiving (US)",
  note: "",
  preset: "thanksgiving_us",
  name: "Thanksgiving",
  emoji: "🦃",
  rule: { kind: "nth", month: 11, day: 1, nth: 4, weekday: 3, offset: 0 },
  lead_days: 14,
  after_days: 0,
  tags: [{ id: 4543, name: "thanksgiving" }],
  genre: null,
  excluded_genres: [],
  collections: [],
  picks: [],
};

export const FATHERS_DAY_AU: SeasonPreset = {
  key: "fathers_day_au",
  label: "Father's Day (AU, NZ)",
  note: "TMDB tags very few films as Father's Day — add a collection or your own picks.",
  preset: "fathers_day_au",
  name: "Father's Day",
  emoji: "👔",
  rule: { kind: "nth", month: 9, day: 1, nth: 1, weekday: 6, offset: 0 },
  lead_days: 7,
  after_days: 0,
  tags: [{ id: 195439, name: "father's day" }],
  genre: null,
  excluded_genres: [],
  collections: [],
  picks: [],
};

export function preview(patch: Partial<SeasonPreview> = {}): SeasonPreview {
  return {
    next_date: "2026-11-26",
    rule_error: null,
    total: 0,
    movies: null,
    shows: null,
    from_tags: 0,
    from_genre: 0,
    from_collections: 0,
    from_picks: 0,
    per_tag: {},
    per_collection: [],
    sample: [],
    ...patch,
  };
}
