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
    highlights: ["One row each", "Follows your global defaults"],
    values: {
      name: "Picked for You",
      name_template: "✨ {library_name} Picked for You",
      build: "per_person",
      size: 15,
    },
  },
  {
    id: "because-you-watched",
    emoji: "🎯",
    title: "Because you watched…",
    blurb:
      "Names one recent watch and fills the row with things like it. The title tells them why it's there.",
    highlights: ["Built from 1 watch", "Named after that title"],
    values: {
      name: "Because you watched",
      name_template: "🎯 Because you watched {top_seed}",
      build: "per_person",
      // 1 seed is the whole point: at the default 30 the row names one watch and fills itself from
      // the other 29, so the title claims something the contents don't honour.
      max_seeds: 1,
      recent_count: 3,
      media: "movie",
      size: 20,
    },
  },
  {
    id: "seen-it-already",
    emoji: "☕",
    title: "Happy to see again",
    // NOT "Comfort rewatch". `watched_pct` is a CEILING, not a preference: `_apply_watched_cap` shows
    // unwatched titles FIRST and merely PERMITS up to `pct` already-finished ones. At 1.0 a library
    // with plenty of unwatched candidates still yields a mostly-unwatched row, so a name promising a
    // rewatch shelf would be wrong most nights. A genuine "prefer rewatches" row needs an engine mode
    // that does not exist yet.
    blurb:
      "The one row that doesn't skip things they've finished — rewatches are allowed to fill it rather than being filtered out.",
    highlights: ["Rewatches allowed", "Changes slowly"],
    values: {
      name: "Happy to see again",
      name_template: "☕ {library_name} you've already seen",
      build: "per_person",
      watched_pct: 1,
      freshness: 0.25,
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
      name: "Fresh finds",
      name_template: "🌱 New {library_name} to try",
      build: "per_person",
      freshness: 1,
      watched_pct: 0,
      size: 15,
    },
  },
  {
    id: "from-the-vault",
    emoji: "🕰️",
    title: "From the vault",
    blurb:
      "Built once and left alone. A shelf that stays put, for a curated set you don't want reshuffled.",
    highlights: ["Never rebuilds on its own", "Pin it and forget it"],
    values: {
      name: "From the vault",
      name_template: "🕰️ {library_name} from the vault",
      build: "per_person",
      freshness: 0,
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
      name: "Popular on this server",
      name_template: "👥 Popular {library_name} on this server",
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
    highlights: ["Movies only", "10 picks"],
    values: {
      name: "Movie night",
      name_template: "🍿 Tonight's {library_name}",
      build: "per_person",
      media: "movie",
      size: 10,
      freshness: 0.5,
    },
  },
  {
    id: "more-tv",
    emoji: "📺",
    title: "More TV to watch",
    // Deliberately NOT "your next series". That promises a show they have not STARTED, and the engine
    // only excludes shows they have FINISHED (`watched_show_pct`) — one they are three episodes into
    // is still eligible. Naming a capability that does not exist is the kind of small lie that makes
    // a whole feature feel untrustworthy.
    blurb: "Shows only, and nothing they've already finished. A shelf of series to start.",
    highlights: ["TV only", "Nothing already finished"],
    values: {
      name: "More TV to watch",
      name_template: "📺 More {library_name} to watch",
      build: "per_person",
      media: "show",
      watched_pct: 0,
      size: 10,
    },
  },
];

export function findRowTemplate(id: string): RowTemplate | undefined {
  return ROW_TEMPLATES.find((template) => template.id === id);
}
