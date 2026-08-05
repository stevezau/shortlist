import { Star } from "lucide-react";

import type { WatchedPage } from "@/lib/types";

/** The page-level facts that decide whether a rating is acting on anything. */
type RatingContext = Pick<WatchedPage, "dislike_threshold" | "ratings_trusted">;

export type RatingVerdict = {
  /** Their rating on Plex's five-star scale — 2/10 reads as 1. */
  stars: number;
  /** This rating is why the title stopped seeding their rows. */
  blocked: boolean;
  /** The rating is displayed but deliberately not acted on, and this says why. */
  ignoredReason: "tool-written" | "distrusted-account" | "switched-off" | null;
};

/** Plex writes whole numbers only, so a fraction means a tool wrote it — see `is_human_rating`
 *  in the backend, which this deliberately mirrors. Kept identical on both sides so the page never
 *  claims a rating is acting when the run ignored it. */
function isHumanRating(rating: number): boolean {
  return Number.isInteger(rating);
}

/** How one person's rating of one title should read, or null if they never rated it.
 *
 *  Exported separately from the component because every branch here is a claim the UI makes about
 *  why a row is or isn't shaping someone's picks, and those are worth testing without rendering.
 */
export function ratingVerdict(
  rating: number | null | undefined,
  page: RatingContext,
): RatingVerdict | null {
  if (rating == null) return null;
  const stars = rating / 2;
  if (!isHumanRating(rating))
    return { stars, blocked: false, ignoredReason: "tool-written" };
  if (!page.ratings_trusted)
    return { stars, blocked: false, ignoredReason: "distrusted-account" };
  if (page.dislike_threshold === null)
    return { stars, blocked: false, ignoredReason: "switched-off" };
  return {
    stars,
    blocked: rating <= page.dislike_threshold,
    ignoredReason: null,
  };
}

const IGNORED_TITLE: Record<
  NonNullable<RatingVerdict["ignoredReason"]>,
  string
> = {
  "tool-written":
    "Not a rating anyone typed — Plex only writes whole numbers, so another tool set this one. It doesn’t affect their picks.",
  "distrusted-account":
    "Another tool is writing ratings on this account, so Shortlist ignores all of them.",
  "switched-off":
    "Plex ratings are switched off in Settings, so this doesn’t affect their picks.",
};

/** What someone rated a title, shown beside the watch it belongs to.
 *
 *  Renders nothing at all for the ~99.7% of watches nobody rated, rather than an empty placeholder:
 *  a column of blanks would imply the list is mostly missing data when it is mostly just unrated.
 */
export function WatchRating({
  rating,
  page,
}: {
  rating: number | null | undefined;
  page: RatingContext;
}) {
  const verdict = ratingVerdict(rating, page);
  if (!verdict) return null;

  const label = `${verdict.stars} out of 5`;
  if (verdict.blocked) {
    return (
      <span
        className="inline-flex items-center gap-1 text-destructive-text"
        title="They rated this low in Plex, so it isn’t used to find similar titles for them. They can change that themselves in Plex."
      >
        <Star className="h-3 w-3 fill-current" aria-hidden />
        {verdict.stars} · not seeding
        <span className="sr-only">
          — rated {label}, so it no longer shapes their picks
        </span>
      </span>
    );
  }
  return (
    <span
      className={
        verdict.ignoredReason
          ? "inline-flex items-center gap-1 text-muted-foreground/60"
          : "inline-flex items-center gap-1 text-primary"
      }
      title={
        verdict.ignoredReason
          ? IGNORED_TITLE[verdict.ignoredReason]
          : `They rated this ${label} in Plex`
      }
    >
      <Star className="h-3 w-3 fill-current" aria-hidden />
      {verdict.stars}
      <span className="sr-only">— rated {label}</span>
    </span>
  );
}
