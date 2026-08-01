/**
 * The rating services Shortlist can read a score from, shared by every place that offers the choice.
 *
 * Two settings use this list and they are deliberately separate: `requests.rating_source` gates which
 * missing titles are worth requesting, while `recommendations.rating_source` decides what a row
 * ordered by "Highest rated" sorts on. An owner can reasonably want a strict IMDb floor for requests
 * and still sort rows by TMDB, so they are not one setting — but they are one list.
 *
 * Everything but TMDB comes from MDBList and needs its API key; TMDB's score is already carried on
 * every candidate, which is why it is the default and costs no lookups.
 */
export type RatingSource =
  "tmdb" | "imdb" | "tomatoes" | "metacritic" | "trakt";

export const RATING_SOURCES: RatingSource[] = [
  "tmdb",
  "imdb",
  "tomatoes",
  "metacritic",
  "trakt",
];

export const RATING_LABELS: Record<RatingSource, string> = {
  tmdb: "TMDB",
  imdb: "IMDb",
  tomatoes: "Rotten Tomatoes",
  metacritic: "Metacritic",
  trakt: "Trakt",
};

/** Whether this source needs an MDBList key to work at all. TMDB never does. */
export function needsMdbList(source: RatingSource): boolean {
  return source !== "tmdb";
}

/** Coerce a stored setting to a known source, falling back to TMDB — the one that always works. */
export function asRatingSource(value: unknown): RatingSource {
  return RATING_SOURCES.includes(value as RatingSource)
    ? (value as RatingSource)
    : "tmdb";
}
