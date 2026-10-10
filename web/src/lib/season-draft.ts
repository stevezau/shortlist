import { ApiError } from "@/lib/api";
import type { SeasonRow } from "@/lib/season-verdict";
import { MONTH_NAMES } from "@/lib/seasons";
import type {
  DateRule,
  Season,
  SeasonCollection,
  SeasonInput,
  SeasonPick,
  SeasonPreset,
  SeasonPreviewInput,
  SeasonTag,
} from "@/lib/types";

/**
 * The season editor's form (#137), and the bodies it sends. Everything here mirrors a limit or a
 * message of `shortlist/server/api/seasons.py`, so the editor can say what's wrong before a save the
 * server would refuse.
 */

/** The API's bounds (`seasons.MAX_LEAD_DAYS` / `MAX_AFTER_DAYS` / `MAX_EASTER_OFFSET`, `SeasonIn`). */
export const MAX_LEAD_DAYS = 90;
export const MAX_AFTER_DAYS = 30;
export const MAX_EASTER_OFFSET = 63;
export const MAX_TAGS = 20;
export const MAX_COLLECTIONS = 10;
export const MAX_PICKS = 200;
export const MAX_EXCLUDED_GENRES = 5;
const MAX_EMOJI_CHARS = 8;

/** TMDB's 19 film genres (`/genre/movie/list`). Their ids never change. */
export const TMDB_MOVIE_GENRES: readonly { id: number; name: string }[] = [
  { id: 28, name: "Action" },
  { id: 12, name: "Adventure" },
  { id: 16, name: "Animation" },
  { id: 35, name: "Comedy" },
  { id: 80, name: "Crime" },
  { id: 99, name: "Documentary" },
  { id: 18, name: "Drama" },
  { id: 10751, name: "Family" },
  { id: 14, name: "Fantasy" },
  { id: 36, name: "History" },
  { id: 27, name: "Horror" },
  { id: 10402, name: "Music" },
  { id: 9648, name: "Mystery" },
  { id: 10749, name: "Romance" },
  { id: 878, name: "Science Fiction" },
  { id: 10770, name: "TV Movie" },
  { id: 53, name: "Thriller" },
  { id: 10752, name: "War" },
  { id: 37, name: "Western" },
];

export function genreName(id: number): string {
  return TMDB_MOVIE_GENRES.find((genre) => genre.id === id)?.name ?? `Genre ${id}`;
}

export type SeasonDraft = {
  name: string;
  emoji: string;
  rule: DateRule;
  lead_days: number;
  after_days: number;
  tags: SeasonTag[];
  genre: number | null;
  excluded_genres: number[];
  collections: SeasonCollection[];
  picks: SeasonPick[];
};

type Sources = Pick<SeasonDraft, "rule"> &
  Partial<Pick<SeasonDraft, "tags" | "genre" | "excluded_genres" | "collections" | "picks">>;

const DEFAULT_EMOJI = "🗓️";

export function blankDraft(): SeasonDraft {
  return {
    name: "",
    emoji: DEFAULT_EMOJI,
    rule: { kind: "fixed", month: 1, day: 1, nth: 1, weekday: 0, offset: 0 },
    lead_days: 7,
    after_days: 0,
    tags: [],
    genre: null,
    excluded_genres: [],
    collections: [],
    picks: [],
  };
}

/** A season's (or a preset's) fields as the form holds them. A preset's `key`, `label` and `note` stay
 *  behind: `SeasonIn` refuses any field it doesn't name. */
export function draftFrom(source: Season | SeasonPreset): SeasonDraft {
  const blank = blankDraft();
  return {
    name: source.name,
    emoji: source.emoji,
    rule: { ...blank.rule, ...pickRule(source.rule) },
    lead_days: source.lead_days ?? blank.lead_days,
    after_days: source.after_days ?? blank.after_days,
    tags: (source.tags ?? []).map(pickTag),
    genre: source.genre ?? null,
    excluded_genres: [...(source.excluded_genres ?? [])],
    collections: (source.collections ?? []).map(pickCollection),
    picks: (source.picks ?? []).map(pickPick),
  };
}

function pickRule(rule: DateRule): DateRule {
  return {
    kind: rule.kind,
    month: rule.month,
    day: rule.day,
    nth: rule.nth,
    weekday: rule.weekday,
    offset: rule.offset,
  };
}

function pickTag(tag: SeasonTag): SeasonTag {
  return { id: tag.id, name: tag.name };
}

function pickCollection(collection: SeasonCollection): SeasonCollection {
  return { section_key: collection.section_key, section_title: collection.section_title, title: collection.title };
}

function pickPick(pick: SeasonPick): SeasonPick {
  return { tmdb_id: pick.tmdb_id, media_type: pick.media_type, title: pick.title, year: pick.year ?? null };
}

/** A season's date and sources, and nothing else: what a save sends and a count is made of. */
function sourcesInput(
  source: Sources,
): Pick<SeasonPreviewInput, "rule" | "tags" | "genre" | "excluded_genres" | "collections" | "picks"> {
  return {
    rule: pickRule(source.rule),
    tags: (source.tags ?? []).map(pickTag),
    genre: source.genre ?? null,
    excluded_genres: [...(source.excluded_genres ?? [])],
    collections: (source.collections ?? []).map(pickCollection),
    picks: (source.picks ?? []).map(pickPick),
  };
}

/** What a count needs, and only that, in one shape: a saved season's list entry and the editor opened
 *  on it ask the same question for the same row, so they share one answer. */
export function previewInput(source: Sources, row: Pick<SeasonRow, "media" | "libraryKeys">): SeasonPreviewInput {
  return { ...sourcesInput(source), media: row.media, library_keys: [...row.libraryKeys] };
}

/** The body to save. `preset` names the ready-made season this came from, on create only. */
export function seasonBody(draft: SeasonDraft, preset: string | null): SeasonInput {
  return {
    name: draft.name.trim(),
    emoji: draft.emoji.trim(),
    ...sourcesInput(draft),
    lead_days: draft.rule.kind === "month" ? 0 : draft.lead_days,
    after_days: draft.rule.kind === "month" ? 0 : draft.after_days,
    ...(preset ? { preset } : {}),
  };
}

export function hasSource(draft: Pick<SeasonDraft, "tags" | "genre" | "collections" | "picks">): boolean {
  return draft.tags.length > 0 || draft.genre !== null || draft.collections.length > 0 || draft.picks.length > 0;
}

/** How many days a fixed date's month offers. February offers the 29th, so choosing it says why not. */
export function daysOffered(month: number): number {
  if (month === 2) return 29;
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

/** What `DateRule.validate` would refuse, in its words, for a rule the pickers can make. */
export function ruleProblem(rule: DateRule): string | null {
  if (rule.kind !== "fixed") return null;
  if (rule.month === 2 && rule.day === 29) return "29 February isn't every year — pick 28 February or 1 March.";
  const days = rule.month === 2 ? 28 : daysOffered(rule.month);
  if (rule.day > days) return `${MONTH_NAMES[rule.month - 1]} has ${days} days.`;
  return null;
}

/** The name of another season this one's name clashes with, ignoring case, as the server compares them
 *  (#137 D13: a row's title renders `{season}`, so two seasons of one name would give two rows one title). */
export function nameClash(name: string, catalogue: readonly Season[], editingSlug: string | null): string | null {
  const wanted = name.trim().toLocaleLowerCase();
  if (!wanted) return null;
  return catalogue.find((season) => season.slug !== editingSlug && season.name.toLocaleLowerCase() === wanted)?.name ?? null;
}

export function emojiProblem(emoji: string): string | null {
  const chars = [...emoji.trim()].length;
  if (chars === 0) return "Add an emoji.";
  if (chars > MAX_EMOJI_CHARS) return "Use one emoji — up to 8 characters.";
  return null;
}

/** Everything that stops a save, as short instructions, in the order the form asks for them. */
export function draftProblems(
  draft: SeasonDraft,
  { clash, ruleError }: { clash: string | null; ruleError: string | null },
): string[] {
  const problems: string[] = [];
  if (!draft.name.trim()) problems.push("Add a name.");
  else if (clash) problems.push("Choose a name no other season has.");
  const emoji = emojiProblem(draft.emoji);
  if (emoji) problems.push(emoji);
  if (ruleError) problems.push("Fix the date.");
  if (!hasSource(draft)) problems.push("Add at least one tag, collection or film.");
  return problems;
}

/** The server's 503s for a missing setup step (`api/seasons.py` `_NO_TMDB` / `_NO_PLEX`). */
export const NO_TMDB_KEY = /TMDB API key/;
const NO_PLEX = /Plex isn't connected/;

/** A count or search refused for a missing setup step: trying again can't help until the owner does
 *  something, so the editor offers no Retry for it, and `useSeasonPreview` counts again by itself when
 *  the owner comes back to the tab. */
export function needsSetup(error: unknown): boolean {
  return (
    error instanceof ApiError && error.status === 503 && (NO_TMDB_KEY.test(error.message) || NO_PLEX.test(error.message))
  );
}

export function clampDays(raw: string, max: number): number {
  return Math.min(max, Math.max(0, Math.round(Number(raw) || 0)));
}
