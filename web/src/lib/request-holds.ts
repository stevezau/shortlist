import type { SeasonTag } from "@/lib/types";

/** The server's own cap (`_MAX_HOLD_TAGS`), for tags and studios alike. */
export const MAX_HOLD_TAGS = 50;

/** Which panel of "Don't request these automatically": movies and shows are held on their own picks. */
export type HoldMedia = "movie" | "show";

/** A studio picked as an exception, stored by id with its name so the page needn't ask TMDB again. */
export interface HoldCompany {
  id: number;
  name: string;
}

/** One media type's picks and exceptions, as the editor holds them. */
export interface HoldDraft {
  genres: number[];
  tags: SeasonTag[];
  companies: HoldCompany[];
  /** null = no rating exception. Not 0: 0 would send every match, a value nobody means. */
  minRating: number | null;
}

export const EMPTY_HOLDS: HoldDraft = { genres: [], tags: [], companies: [], minRating: null };

/** TMDB's TV genre list (`/genre/tv/list`) — its own, not the movie one: shows have one genre for
 *  what films split in two (Action & Adventure, Sci-Fi & Fantasy), and five genres films never get. */
export const TMDB_SHOW_GENRES: readonly { id: number; name: string }[] = [
  { id: 10759, name: "Action & Adventure" },
  { id: 16, name: "Animation" },
  { id: 35, name: "Comedy" },
  { id: 80, name: "Crime" },
  { id: 99, name: "Documentary" },
  { id: 18, name: "Drama" },
  { id: 10751, name: "Family" },
  { id: 10762, name: "Kids" },
  { id: 9648, name: "Mystery" },
  { id: 10763, name: "News" },
  { id: 10764, name: "Reality" },
  { id: 10765, name: "Sci-Fi & Fantasy" },
  { id: 10766, name: "Soap" },
  { id: 10767, name: "Talk" },
  { id: 10768, name: "War & Politics" },
  { id: 37, name: "Western" },
];

/** Exact tags that name a kind of film, measured on TMDB 2026-10-09. The broad "concert" tag is left
 *  out on purpose: TMDB puts it on A Star Is Born and Almost Famous too. */
export const SUGGESTED_HOLD_TAGS: readonly SeasonTag[] = [
  { id: 156205, name: "concert film" },
  { id: 11634, name: "live performance" },
  { id: 246377, name: "music documentary" },
  { id: 9716, name: "stand-up comedy" },
  { id: 9817, name: "behind the scenes" },
];

/** Tags that name a kind of show. Narrower than a genre: "anime" leaves every other cartoon alone,
 *  where the Animation genre would hold Bluey too. */
export const SUGGESTED_SHOW_HOLD_TAGS: readonly SeasonTag[] = [
  { id: 210024, name: "anime" },
  { id: 161919, name: "adult animation" },
  { id: 9716, name: "stand-up comedy" },
];

/** Where each panel's picks are stored. The movie keys carry no media word because they predate the
 *  show ones: an upgrade reads them as the movie picks they always were. */
const PREFIX: Record<HoldMedia, string> = { movie: "requests.hold_", show: "requests.hold_show_" };

/** `requests.hold_tags` (and the companies) are stored as `{"<id>": "<name>"}`; the editor works in a list. */
export function readHoldTags(raw: unknown): SeasonTag[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>)
    .filter(([id, name]) => /^\d+$/.test(id) && typeof name === "string")
    .map(([id, name]) => ({ id: Number(id), name: name as string }));
}

export function writeHoldTags(tags: readonly { id: number; name: string }[]): Record<string, string> {
  return Object.fromEntries(tags.map((tag) => [String(tag.id), tag.name]));
}

export function readHoldGenres(raw: unknown): number[] {
  return Array.isArray(raw) ? raw.filter((g): g is number => typeof g === "number") : [];
}

function readRating(raw: unknown): number | null {
  if (raw === null || raw === undefined || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export function readHolds(settings: Record<string, unknown>, media: HoldMedia): HoldDraft {
  const prefix = PREFIX[media];
  return {
    genres: readHoldGenres(settings[`${prefix}genres`]),
    tags: readHoldTags(settings[`${prefix}tags`]),
    companies: readHoldTags(settings[`${prefix}except_companies`]),
    minRating: readRating(settings[`${prefix}except_min_rating`]),
  };
}

export function writeHolds(draft: HoldDraft, media: HoldMedia): Record<string, unknown> {
  const prefix = PREFIX[media];
  return {
    [`${prefix}genres`]: draft.genres,
    [`${prefix}tags`]: writeHoldTags(draft.tags),
    [`${prefix}except_companies`]: writeHoldTags(draft.companies),
    [`${prefix}except_min_rating`]: draft.minRating,
  };
}

/** How many exceptions a panel has, for its collapsed row. */
export function exceptionCount(draft: HoldDraft): number {
  return draft.companies.length + (draft.minRating === null ? 0 : 1);
}

/** "A", "A and B", "A, B and C" — the house list style (no serial comma). */
export function joinTitles(titles: readonly string[]): string {
  if (titles.length <= 1) return titles.join("");
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}
