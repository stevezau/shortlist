import { blankInput } from "@/lib/collections";
import type { RowKindContext } from "@/lib/row-kinds";
import type { Collection } from "@/lib/types";

/**
 * Every shape of row the owner's server could hold, one per kind and one per edge (design §2).
 * Shared by the editor's kind tests and the "What this row will do" tests, so both hold the same rows
 * to the same kind→settings map.
 */

/** What the editor resolves the globals to when settings carry none: the server's own defaults. */
export const CTX: RowKindContext = {
  isDefault: false,
  globalMaxSeeds: 30,
  defaultRowName: "✨ {library_name} Picked for You",
  globalSources: ["tmdb_similar", "tmdb_discover"],
  seasonCatalogue: ["valentines", "halloween", "christmas"],
  builtinSeasons: ["valentines", "halloween", "christmas"],
};

export const BYW_NAME = "🎯 Because you watched {top_seed}";

export function row(patch: Partial<Collection> = {}): Collection {
  return {
    ...(blankInput() as unknown as Collection),
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    shown_today: true,
    season_status: null,
    poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
    ...patch,
  };
}

export function named(name: string): Partial<Collection> {
  return { name, name_template: name };
}

/** Every inheritable field set, so nothing about opening the row can hide behind a null. */
export const EVERY_OVERRIDE: Partial<Collection> = {
  ...named("✨ {library_name} Picks"),
  watched_pct: 0.3,
  refresh_days: 5,
  idle_hold_days: 10,
  recency: 0.4,
  recent_count: 6,
  max_seeds: 12,
  cold_start: "skip",
  candidate_sources: ["tmdb_similar", "llm_web"],
  pick_order: "rating",
  description: "Picked nightly",
  sort_title_prefix: "!010_",
  show_days: [1, 3],
  placement: "library",
  placement_friends: "home",
  // What the API returns for a library pinned to the top; the editor keeps it as it is.
  hub_anchor: { "1": { top: true } } as unknown as Collection["hub_anchor"],
  request_tag: "family",
  req_min_rating: 6.5,
  req_min_votes: 200,
  req_min_demand: 2,
  req_min_year: 1990,
  req_max_year: 2020,
  req_auto_send: true,
  req_auto_min_demand: 3,
  req_auto_min_rating: 7.5,
  req_max_per_row: 4,
  req_radarr_quality_profile_id: 3,
  req_radarr_root_folder: "/data/Kids Movies",
  req_sonarr_quality_profile_id: 5,
  req_sonarr_root_folder: "/data/Kids TV",
  req_sonarr_monitor: "firstSeason",
  req_language_mode: "prefer",
  req_preferred_languages: ["en", "fr"],
  req_min_rating_other: 8,
  req_auto_user_tag: false,
};

export const FIXTURES: [string, Collection, RowKindContext][] = [
  ["Picked for You", row(named("✨ {library_name} Picks")), CTX],
  ["Because you watched", row({ ...named(BYW_NAME), max_seeds: 2 }), CTX],
  ["Watch it again", row({ ...named("☕ {library_name} you've already seen"), rewatch: true, watched_pct: 1 }), CTX],
  ["Popular on this server", row({ ...named("👥 Popular {library_name}"), build: "shared", min_watchers: 3 }), CTX],
  ["Your requests", row({ ...named("📬 {library_name} you asked for"), requests_row: true, requests_window_days: 90 }), CTX],
  ["Seasonal", row({ ...named("{season_emoji} {season} picks"), seasons: ["halloween"] }), CTX],
  [
    "row 2's live state",
    row({ ...named(BYW_NAME), max_seeds: 3, seed_window: 1, media: "both", refresh_days: 1 }),
    CTX,
  ],
  ["the default row", row({ slug: "picked", name: "✨ {library_name} Picked for You" }), { ...CTX, isDefault: true }],
  ["seasonal + shared", row({ ...named("{season} for everyone"), seasons: ["christmas"], build: "shared" }), CTX],
  ["seasonal + rewatch", row({ ...named("{season} again"), seasons: ["christmas"], rewatch: true, watched_pct: 1 }), CTX],
  ["seasonal + {top_seed}", row({ ...named(BYW_NAME), seasons: ["halloween", "christmas"] }), CTX],
  ["a Because you watched blend taking turns", row({ ...named(BYW_NAME), max_seeds: 3, seed_window: 3 }), CTX],
  ["Picked for You still taking turns", row({ ...named("✨ {library_name} Picks"), seed_window: 2 }), CTX],
  [
    "Watch it again taking turns on its new picks",
    row({ ...named("☕ {library_name} you've already seen"), rewatch: true, watched_pct: 1, max_seeds: 2, seed_window: 3 }),
    CTX,
  ],
  ["movie-only", row({ ...named("🍿 Tonight's {library_name}"), media: "movie" }), CTX],
  ["show-only", row({ ...named(BYW_NAME), media: "show", max_seeds: 1 }), CTX],
  ["every inheritable field overridden", row(EVERY_OVERRIDE), CTX],
];
