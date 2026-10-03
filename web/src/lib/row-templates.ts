import { KIND_META, ROW_KINDS, type RowKind } from "@/lib/row-kind-meta";
import type { CollectionInput } from "@/lib/types";

/**
 * Ready-made rows, so "Add a row" is a choice rather than a blank 17-field form.
 *
 * The form is the only place the app ever explained what a row COULD be, and it explained it one
 * control at a time — you had to already know what you wanted to build before it helped. These are
 * the answer to "what can I make here?". `summary` keeps the chooser compact, while `highlights`
 * names the settings each template changes in the selected template's details.
 *
 * `values` is deliberately a partial: everything it omits keeps `blankInput()`'s default, and every
 * field stays editable after picking. A template is a starting point, never a mode.
 *
 * `kind` groups the gallery by the same six kinds the row editor's kind picker uses (design doc §3),
 * with the picker's own copy, so the two cannot describe a kind differently.
 */

export interface RowTemplate {
  id: string;
  kind: RowKind;
  emoji: string;
  title: string;
  summary: string;
  blurb: string;
  /** The settings this template changes, in plain English. */
  highlights: string[];
  values: Partial<CollectionInput>;
}

/** The gallery's six headings, in the kind picker's order, each with the kind's description. */
export const ROW_TEMPLATE_GROUPS: {
  kind: RowKind;
  heading: string;
  description: string;
}[] = ROW_KINDS.map((kind) => ({
  kind,
  heading: KIND_META[kind].title,
  description: KIND_META[kind].description,
}));

export const ROW_TEMPLATES: RowTemplate[] = [
  {
    id: "picked-for-you",
    kind: "picked",
    emoji: "✨",
    title: "Picked for You",
    summary: "A little of everything they love",
    blurb:
      "The everyday row. Blends someone's whole recent history into a general set of suggestions.",
    // "15 picks" rather than the "follows your global defaults" this used to claim. Size is the one
    // setting a row can NEVER inherit: `RowSpec.size` is a plain int with no None, and only the
    // DEFAULT row reads the global (`context_builder`: `size=store.get("row.size") if is_default`).
    // So a server whose global row size is 25 still got a 15 from this tile, under a chip promising
    // otherwise. Everything else here really is null and really does inherit.
    highlights: [
      "One row each",
      "15 picks",
      "Other settings from your defaults",
    ],
    values: {
      // NOT "✨ {library_name} Picked for You". That is the DEFAULT row's title — the global
      // `row.name_template` every install ships with — so this tile used to add a second row that
      // rendered the identical Plex title, under the identical per-user label, and the two silently
      // shared one collection. `reconcile.row_titled_from` now refuses that pair, which would leave
      // this tile 422-ing on every server that still has its default row. A distinct name is what
      // makes it savable, and it stays editable in the form underneath.
      name: "✨ {library_name} Picks",
      build: "per_person",
      size: 15,
    },
  },
  {
    id: "because-you-watched",
    kind: "byw",
    emoji: "🎯",
    title: "Because you watched…",
    summary: "One favourite leads to another",
    blurb:
      "Names one recent film and fills the row with things like it. The title tells them why it's there.",
    // "Films only" is first because it is the one thing about this template someone would not guess:
    // a single seed can only cover ONE media type (seeds balance across the types present), so a
    // one-seed row has to pick one. Leaving it unsaid meant the card promised a row about "a recent
    // watch" and quietly delivered a movies-only one.
    highlights: [
      "Films only",
      // Worded as the row card badges it ("All sources: 1 watch"), so the gallery and the list do
      // not name one setting two ways.
      "All sources: 1 watch",
      "Follows their latest watch",
    ],
    values: {
      name: "🎯 Because you watched {top_seed}",
      build: "per_person",
      // 1 seed is the whole point: at the default 30 the row names one watch and fills itself from
      // the other 29, so the title claims something the contents don't honour.
      max_seeds: 1,
      recent_count: 1,
      media: "movie",
      size: 20,
      // Nightly, not the global default. This row is ABOUT recency: at the default cadence (~8 days)
      // it keeps naming last week's film for a week after they've moved on, which reads as broken
      // rather than as a setting.
      //
      // Nightly costs more than "a write when the seed moves": on the nights the seed has NOT moved
      // the row still takes the refresh branch, which swaps its weakest third and writes. So this row
      // writes to Plex most nights, per user per library, where the global default wrote weekly. It
      // costs no extra AI usage — candidates are gathered once a run whatever a row's cadence is.
      //
      // (The engine now forces this for any `{top_seed}` row, so it also holds for rows made before
      // this default existed — but it stays here so the value the card shows is the value it saves.)
      refresh_days: 1,
      // Their most recent watch, which is the behaviour this template was asked for (issue #57: "a
      // Netflix-style row about ONE thing they watched"). Cycling between several is one number away
      // in the editor, but it is not the default: it rebuilds the row and writes to Plex most nights,
      // which is a cost every user of this template should opt into rather than inherit.
      seed_window: 1,
    },
  },
  {
    id: "seen-it-already",
    kind: "again",
    emoji: "☕",
    title: "Watch it again",
    summary: "Favourites worth another look",
    // `watched_pct` alone could never deliver this: it is a CEILING, so `_apply_watched_cap` shows
    // unwatched titles FIRST and merely PERMITS finished ones — at 1.0 a library with plenty of
    // unwatched candidates still yielded a mostly-unwatched row. `rewatch` (engine) inverts that
    // preference, which is what lets this template claim a rewatch shelf honestly.
    blurb:
      "Films and shows they've already finished, put back in front of them — a shelf of old favourites rather than new suggestions.",
    highlights: ["Rewatches come first", "Changes slowly"],
    values: {
      name: "☕ {library_name} you've already seen",
      build: "per_person",
      // `rewatch` is what delivers this row: the engine builds it from their finished titles. The
      // engine ignores `watched_pct` on a rewatch row; 1 keeps the slider honest ("already-watched
      // titles can fill the whole row") if someone opens the editor.
      rewatch: true,
      watched_pct: 1,
      refresh_days: 11,
      size: 15,
    },
  },
  {
    id: "your-requests",
    kind: "requests",
    emoji: "📬",
    title: "Your requests",
    summary: "What they asked for, ready to watch",
    blurb:
      "What they asked for in Overseerr, once it's on Plex. Each title leaves once they've watched it.",
    highlights: ["Only what they asked for", "Newest first", "Overseerr or Radarr/Sonarr tags"],
    values: {
      name: "📬 {library_name} you asked for",
      build: "per_person",
      requests_row: true,
      // 90 days: long enough that a request they made last season is still there, short enough that
      // one they've lost interest in doesn't sit in the row for good.
      requests_window_days: 90,
      size: 20,
    },
  },
  {
    id: "fresh-finds",
    kind: "picked",
    emoji: "🌱",
    title: "Fresh finds",
    summary: "Something new, every evening",
    blurb:
      "Titles refresh every night, nothing they've seen. For people who want something new each evening.",
    highlights: ["Refreshes nightly", "Nothing already watched"],
    values: {
      name: "🌱 New {library_name} to try",
      build: "per_person",
      refresh_days: 1,
      // This template promises new picks nightly even when their watch history has not changed.
      idle_hold_days: 0,
      watched_pct: 0,
      size: 15,
    },
  },
  {
    id: "seasonal",
    kind: "seasonal",
    emoji: "🗓️",
    title: "Seasonal",
    summary: "The right films at the right time",
    // Films only: TMDB tags a few dozen seasonal SHOWS against thousands of films (13 Christmas shows
    // on a 5,000-show library, measured for discussion #124), so a TV half would sit nearly empty.
    blurb:
      "One shared row of the most-watched seasonal films. Follows the holidays you choose and stays hidden between seasons.",
    highlights: [
      "Shared with everyone",
      "Needs 2 watchers",
      "Halloween, Christmas & Valentine's, or your own",
      "Shows a month before",
      "Rebuilt nightly",
    ],
    values: {
      name: "{season_emoji} {season} picks",
      build: "shared",
      min_watchers: 2,
      media: "movie",
      size: 15,
      seasons: ["valentines", "halloween", "christmas"],
      season_lead_days: 30,
      season_after_days: 0,
      // Shared popularity is recounted every run. Keep a nightly cadence if they choose Per person.
      refresh_days: 1,
      // Personal seasonal picks should keep older favourites competitive too. Shared ignores age.
      recency: 0,
    },
  },
  {
    id: "from-the-vault",
    kind: "picked",
    emoji: "🕰️",
    title: "From the vault",
    summary: "A shelf that takes its time",
    // A frozen row still replaces watched picks. Pin the cap so an owner's more permissive global
    // default cannot silently turn off the replacement this template promises.
    blurb:
      "Built once and never re-picked on a schedule. A shelf that stays put apart from titles they've watched, which are replaced.",
    highlights: ["Never refreshes on its own", "Only moves as they watch it"],
    values: {
      name: "🕰️ {library_name} from the vault",
      build: "per_person",
      refresh_days: 0,
      watched_pct: 0,
      size: 20,
    },
  },
  {
    id: "popular-here",
    kind: "popular",
    emoji: "👥",
    title: "Popular on this server",
    summary: "What everyone’s watching",
    blurb:
      "One row everybody sees, built only from titles several people have watched. Nothing personal in it.",
    highlights: ["Shared with everyone", "Needs 3 watchers"],
    values: {
      name: "👥 Popular {library_name} on this server",
      build: "shared",
      min_watchers: 3,
      size: 20,
    },
  },
  {
    id: "movie-night",
    kind: "picked",
    emoji: "🍿",
    title: "Movie night",
    summary: "Ten films. One good evening.",
    blurb:
      "Ten films picked for each person, refreshed weekly. A short list for their next movie night.",
    highlights: ["Movies only", "10 picks", "Weekly"],
    values: {
      name: "🍿 Tonight's {library_name}",
      build: "per_person",
      media: "movie",
      size: 10,
      // The blurb says "refreshed weekly", so the cadence is 7. This needed a comment when it was a
      // fraction: 0.5 resolved to 8 days, a day out, so the value had to be nudged to 0.53.
      refresh_days: 7,
      // A weekly shelf keeps its cadence even if the global idle hold is longer than a week.
      idle_hold_days: 0,
    },
  },
  {
    id: "more-tv",
    kind: "picked",
    emoji: "📺",
    title: "More TV to watch",
    summary: "Their next series starts here",
    // Now literally true: `unstarted_only` (engine) drops any series with a single viewed episode,
    // where the normal filter only drops FINISHED ones — so a show they are three episodes into no
    // longer turns up on a shelf that calls itself "to start".
    blurb:
      "Series they have never opened — not one they're part-way through. A shelf of things to start.",
    highlights: ["TV only", "Never started"],
    values: {
      name: "📺 More {library_name} to watch",
      build: "per_person",
      media: "show",
      // Redundant TODAY and kept deliberately: at `watched_pct: 0` below, `zero_pct_exclusions`
      // already unions the started shows in, so this flag changes no candidate (the `pool_key`
      // comment in rows.py says so, and is why the two don't even split the pool). It is what makes
      // the "Never started" claim survive the owner raising the watched cap on this row — the one
      // edit that would otherwise turn the tile's promise off silently. Don't delete it as dead.
      unstarted_only: true,
      watched_pct: 0,
      size: 10,
    },
  },
];

export function findRowTemplate(id: string): RowTemplate | undefined {
  return ROW_TEMPLATES.find((template) => template.id === id);
}

/** Names a highlight may start with that keep their capital mid-sentence: the apps, and every season
 *  the engine ships (`shortlist/engine/seasons.py`), matched on the first word. */
const PROPER_NOUNS = new Set([
  "Overseerr",
  "Radarr",
  "Sonarr",
  "Plex",
  "TMDB",
  "Trakt",
  "TV",
  "Halloween",
  "Christmas",
  "Valentine's",
]);

/**
 * The highlights as they read joined into one sentence: each starts lowercase, unless its first
 * word is a proper noun or an acronym (a run of two or more capitals). A blanket `toLowerCase()`
 * wrote "overseerr or radarr/sonarr tags" and "tv only" in the editor's "Started from" banner.
 */
export function sentenceCaseHighlights(highlights: string[]): string[] {
  return highlights.map((highlight) => {
    const firstWord = highlight.split(/[\s/,.-]/, 1)[0] ?? "";
    const keepsCapital = PROPER_NOUNS.has(firstWord) || /^[A-Z]{2,}/.test(firstWord);
    return keepsCapital ? highlight : highlight.charAt(0).toLowerCase() + highlight.slice(1);
  });
}
