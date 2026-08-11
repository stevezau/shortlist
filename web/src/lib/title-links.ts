/**
 * "Look it up" links for a title, by its TMDB id.
 *
 * Shared rather than rebuilt per page: the requests inbox has had these since it shipped, and the
 * run report grew the same need — the moment a row shows a year and a score, the next question is
 * "what IS this?". One builder means the two screens cannot disagree about where a title lives.
 *
 * IMDb is a title SEARCH unless an IMDb id is known. Shortlist only resolves those for titles it
 * considers requesting (`MissingTitle.imdb_id`); a delivered pick carries a TMDB id and nothing
 * else, and guessing an IMDb id from a title would be worse than searching for it.
 */
export interface TitleLink {
  label: "TMDB" | "IMDb" | "Trakt";
  href: string;
}

export function titleLinks(title: {
  tmdb_id?: number | null;
  media_type?: string | null;
  title?: string | null;
  imdb_id?: string | null;
}): TitleLink[] {
  if (!title.tmdb_id) return []; // a cold-start pick has no TMDB id — no link is better than a broken one
  const tmdbPath = title.media_type === "movie" ? "movie" : "tv";
  const traktType = title.media_type === "movie" ? "movie" : "show";
  return [
    {
      label: "TMDB",
      href: `https://www.themoviedb.org/${tmdbPath}/${title.tmdb_id}`,
    },
    {
      label: "IMDb",
      href: title.imdb_id
        ? `https://www.imdb.com/title/${title.imdb_id}/`
        : `https://www.imdb.com/find/?q=${encodeURIComponent(title.title ?? "")}&s=tt`,
    },
    {
      label: "Trakt",
      href: `https://trakt.tv/search/tmdb/${title.tmdb_id}?id_type=${traktType}`,
    },
  ];
}
