import type { SeasonTag } from "@/lib/types";

/** The server's own cap (`_MAX_HOLD_TAGS`). */
export const MAX_HOLD_TAGS = 50;

/** Exact tags that name a kind of film, measured on TMDB 2026-10-09. The broad "concert" tag is left
 *  out on purpose: TMDB puts it on A Star Is Born and Almost Famous too. */
export const SUGGESTED_HOLD_TAGS: readonly SeasonTag[] = [
  { id: 156205, name: "concert film" },
  { id: 11634, name: "live performance" },
  { id: 246377, name: "music documentary" },
  { id: 9716, name: "stand-up comedy" },
  { id: 9817, name: "behind the scenes" },
];

/** `requests.hold_tags` is stored as `{"<id>": "<name>"}`; the editor works in a list. */
export function readHoldTags(raw: unknown): SeasonTag[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>)
    .filter(([id, name]) => /^\d+$/.test(id) && typeof name === "string")
    .map(([id, name]) => ({ id: Number(id), name: name as string }));
}

export function writeHoldTags(tags: SeasonTag[]): Record<string, string> {
  return Object.fromEntries(tags.map((tag) => [String(tag.id), tag.name]));
}

export function readHoldGenres(raw: unknown): number[] {
  return Array.isArray(raw) ? raw.filter((g): g is number => typeof g === "number") : [];
}
