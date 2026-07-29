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
    id: "comfort-rewatch",
    emoji: "☕",
    title: "Comfort rewatch",
    blurb:
      "Things they've already finished and would happily put on again. The one row where already-watched is the point.",
    highlights: ["100% already-watched", "Changes slowly"],
    values: {
      name: "Comfort rewatch",
      name_template: "☕ Comfort rewatch",
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
      name_template: "🌱 Fresh finds",
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
      name_template: "🕰️ From the vault",
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
      name_template: "👥 Popular on this server",
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
      name_template: "🍿 Movie night",
      build: "per_person",
      media: "movie",
      size: 10,
      freshness: 0.5,
    },
  },
  {
    id: "next-series",
    emoji: "📺",
    title: "Your next series",
    blurb:
      "TV only. For the moment someone finishes a show and wants the next one.",
    highlights: ["TV only", "10 picks"],
    values: {
      name: "Your next series",
      name_template: "📺 Your next series",
      build: "per_person",
      media: "show",
      size: 10,
    },
  },
];

export function findRowTemplate(id: string): RowTemplate | undefined {
  return ROW_TEMPLATES.find((template) => template.id === id);
}
