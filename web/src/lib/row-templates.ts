import type { CollectionInput } from "@/lib/types";

/**
 * Ready-made rows, so "Add a row" is a choice rather than a blank 17-field form.
 *
 * The form is the only place the app ever explained what a row COULD be, and it explained it one
 * control at a time — you had to already know what you wanted to build before it helped. These are
 * the answer to "what can I make here?", and `highlights` names the two or three settings each one
 * actually changes, so picking a template teaches the knobs rather than hiding them.
 *
 * `values` is deliberately a partial: everything it omits keeps `blankInput()`'s default, and every
 * field stays editable after picking. A template is a starting point, never a mode.
 */
export interface RowTemplate {
  id: string;
  emoji: string;
  title: string;
  blurb: string;
  /** The settings this template changes, in plain English, for the tile. */
  highlights: string[];
  values: Partial<CollectionInput>;
}

export const ROW_TEMPLATES: RowTemplate[] = [
  {
    id: "picked-for-you",
    emoji: "✨",
    title: "Picked for You",
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
    emoji: "🎯",
    title: "Because you watched…",
    blurb:
      "Names one recent film and fills the row with things like it. The title tells them why it's there.",
    // "Films only" is first because it is the one thing about this template someone would not guess:
    // a single seed can only cover ONE media type (seeds balance across the types present), so a
    // one-seed row has to pick one. Leaving it unsaid meant the card promised a row about "a recent
    // watch" and quietly delivered a movies-only one.
    highlights: [
      "Films only",
      "Built from 1 watch",
      "Follows their latest watch",
    ],
    values: {
      name: "🎯 Because you watched {top_seed}",
      build: "per_person",
      // 1 seed is the whole point: at the default 30 the row names one watch and fills itself from
      // the other 29, so the title claims something the contents don't honour.
      max_seeds: 1,
      recent_count: 3,
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
    emoji: "☕",
    title: "Happy to see again",
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
      // `rewatch` is what actually delivers this row; `watched_pct: 1` only stops the pool from
      // dropping finished titles before the ordering ever sees them.
      rewatch: true,
      watched_pct: 1,
      refresh_days: 11,
      size: 15,
    },
  },
  {
    id: "fresh-finds",
    emoji: "🌱",
    title: "Fresh finds",
    blurb:
      "Rebuilds every night, nothing they've seen. For people who want something new each evening.",
    highlights: ["Rebuilds nightly", "Nothing already watched"],
    values: {
      name: "🌱 New {library_name} to try",
      build: "per_person",
      refresh_days: 1,
      watched_pct: 0,
      size: 15,
    },
  },
  {
    id: "from-the-vault",
    emoji: "🕰️",
    title: "From the vault",
    // "Never re-picks on a schedule" is the honest form of what `refresh_days: 0` buys. The row is
    // frozen against the CADENCE, not against everything: it inherits the global `watched_pct`, which
    // defaults to 0, and a 0% row drops any pick the person has since watched (`_reusable_prior`) —
    // the carry-forward branch then pads the gap from that night's pool. So the shelf does move, for
    // exactly the people watching from it, and the old "set it once, it stays" promised otherwise.
    blurb:
      "Built once and never re-picked on a schedule. A shelf that stays put apart from titles they've watched, which are replaced.",
    highlights: ["Never rebuilds on its own", "Only moves as they watch it"],
    values: {
      name: "🕰️ {library_name} from the vault",
      build: "per_person",
      refresh_days: 0,
      size: 20,
    },
  },
  {
    id: "popular-here",
    emoji: "👥",
    title: "Popular on this server",
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
    emoji: "🍿",
    title: "Movie night",
    blurb:
      "Films only, a short shelf, refreshed weekly. Something to pick from on a Friday.",
    highlights: ["Movies only", "10 picks", "Weekly"],
    values: {
      name: "🍿 Tonight's {library_name}",
      build: "per_person",
      media: "movie",
      size: 10,
      // The blurb says "refreshed weekly", so the cadence is 7. This needed a comment when it was a
      // fraction: 0.5 resolved to 8 days, a day out, so the value had to be nudged to 0.53.
      refresh_days: 7,
    },
  },
  {
    id: "more-tv",
    emoji: "📺",
    title: "More TV to watch",
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
