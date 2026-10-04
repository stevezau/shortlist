/**
 * The row kinds and their copy (design `.claude/docs/plans/row-editor-cleanup-design.md` §3), shared by
 * the editor's kind picker and the new-row gallery so the two cannot drift.
 *
 * A leaf module on purpose: `row-kinds.ts` imports `row-templates.ts` for its template names, so the
 * gallery reading this copy from `row-kinds.ts` would be a circular import that breaks depending on
 * which module loads first.
 */

export type RowKind = "picked" | "byw" | "again" | "requests" | "seasonal" | "popular";

/** How a row is filled. A seasonal row has one of these too; every other row's fill is its kind. */
export type RowFill = Exclude<RowKind, "seasonal">;

export interface RowKindChoice {
  kind: RowKind;
  /** Read only when `kind` is "seasonal"; any other kind is its own fill. */
  fill: RowFill;
}

export interface KindMeta {
  title: string;
  /** One line on what viewers see. */
  description: string;
}

/** The picker's order. */
export const ROW_KINDS: readonly RowKind[] = ["picked", "byw", "again", "requests", "seasonal", "popular"];

/** Every fill, in the picker's order. */
export const ROW_FILLS: readonly RowFill[] = ["picked", "byw", "again", "requests", "popular"];

/**
 * The Seasonal block's "How it's filled" order. A requests row is never seasonal — a request lands
 * when it lands, and the API refuses the pair — so that one fill is left out.
 */
export const SEASONAL_FILLS: readonly RowFill[] = ROW_FILLS.filter((fill) => fill !== "requests");

export const KIND_META: Readonly<Record<RowKind, KindMeta>> = {
  picked: {
    title: "Picked for You",
    description: "Titles they haven't seen yet, matched to everything they like.",
  },
  byw: {
    title: "Because you watched",
    description: 'More like one thing they watched recently. Named after it, like "Because you watched Dune".',
  },
  again: {
    title: "Watch it again",
    description: "Favourites they've already finished, ready to rewatch.",
  },
  requests: {
    title: "Your requests",
    description: "What they asked for in Overseerr that's now on Plex, newest first. Never recommendations.",
  },
  seasonal: {
    title: "Seasonal",
    description:
      "Only appears around the holidays you pick, like Halloween or Christmas, or a season you add yourself. Filled in any of the ways above.",
  },
  popular: {
    title: "Popular on this server",
    description: "What lots of people here are watching. Everyone sees the same row.",
  },
};

export const FILL_META: Readonly<Record<RowFill, KindMeta>> = {
  picked: KIND_META.picked,
  byw: KIND_META.byw,
  again: KIND_META.again,
  requests: KIND_META.requests,
  popular: KIND_META.popular,
};

export const KIND_GROUP: Readonly<KindMeta> = {
  title: "What kind of row is this?",
  description:
    "Each kind fills the row in a different way. Pick one, and the settings below change to match it. You can switch later: you'll see exactly what will change before anything is saved.",
};

/**
 * An AI row (#138): filled from a theme the AI wrote once from the owner's description. Not in
 * `KIND_META` or the picker's list: a row can't be switched into it (it needs a theme first), so it
 * is chosen from the gallery's "AI" group, and the editor shows it as a fixed row type.
 */
export const AI_KIND_META: Readonly<KindMeta> = {
  title: "AI row",
  description:
    "Describe the row in your own words. The AI writes the list once, then Shortlist picks from it for each person every run, with no more AI.",
};
