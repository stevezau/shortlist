/**
 * The one look for "this one is chosen": a raised neutral surface with a 2px amber edge.
 *
 * Filled amber is reserved for the single primary action on a screen. When it also marked the
 * active nav item, the selected tab, segments, preset chips and rank numbers, amber meant both
 * "selected" and "do this", and the eye could not tell which. Every selected state uses one of
 * these two recipes instead, so a new control picks one rather than inventing a third.
 */

/** Horizontal controls (segments, preset chips, filter chips): the edge runs along the bottom. */
export const selectedClass = "border-border bg-raised text-foreground shadow-selected-x";

/** Vertical lists (the nav rail, the settings sub-nav, a person list): the edge runs down the left. */
export const selectedVerticalClass = "border-border bg-raised text-foreground shadow-selected-y";

/** The resting state paired with {@link selectedClass}, so unselected options share one look too. */
export const unselectedClass =
  "border-border-strong bg-elevated text-muted-foreground hover:bg-raised hover:text-foreground";
