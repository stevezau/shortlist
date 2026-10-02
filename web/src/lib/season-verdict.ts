import type { CollectionInput } from "@/lib/types";

/** Which library types a row builds in. */
export type RowMedia = CollectionInput["media"];

/** The row the season editor was opened from: what a season's count is made for and judged by (#137 D10).
 *  Only titles of its media type in its libraries count, because that is all it draws from (I-1). Empty
 *  `libraryKeys` is every library of its type. */
export type SeasonRow = {
  size: number;
  perPerson: boolean;
  media: RowMedia;
  libraryKeys: readonly string[];
};

/** What a row's titles are called: a films row draws films, a shows row shows, a row of both titles. */
export function titleNoun(media: RowMedia, count: number): string {
  const [one, many] = media === "movie" ? ["film", "films"] : media === "show" ? ["show", "shows"] : ["title", "titles"];
  return count === 1 ? one : many;
}

/** Below this many titles, per-person rows of one season come out much alike: #124 judged 63 too few to
 *  make them differ. A heuristic, not a measured line (#137 D10). */
export const ALIKE_BELOW = 100;

export type SeasonVerdict = { level: "few" | "alike" | "ok"; text: string };

/** A season's count for one row (`POST /api/seasons/preview`): the total, and its films and shows — null
 *  for a type the row builds in no library of. */
export type SeasonCount = { total: number; movies?: number | null; shows?: number | null };

/** What a season's count means for the row it is being added to (#137 D10). Never blocks a save.
 *
 *  A row of both fills each library from its own type, so it is short when EITHER half is: 40 films and no
 *  shows leave its TV library empty, whatever the total says. */
export function seasonVerdict(count: SeasonCount, row: Pick<SeasonRow, "size" | "perPerson" | "media">): SeasonVerdict {
  const { total } = count;
  const size = row.size;
  if (row.media === "both") {
    const short = (
      [
        ["movie", count.movies, "film library"],
        ["show", count.shows, "TV library"],
      ] as const
    ).filter(([, n]) => n != null && n < size);
    const [only] = short;
    if (short.length === 1 && only) {
      const [media, n, library] = only;
      return { level: "few", text: `Too few ${titleNoun(media, 2)} to fill this row's ${library} (${n} of ${size})` };
    }
    if (short.length === 2) {
      const [movies, shows] = [count.movies ?? 0, count.shows ?? 0];
      return {
        level: "few",
        text: `Too few films or shows to fill this row's libraries (${movies} ${titleNoun("movie", movies)} and ${shows} ${titleNoun("show", shows)}, of ${size} each)`,
      };
    }
  }
  const titles = titleNoun(row.media, 2);
  if (total < size) return { level: "few", text: `Too few ${titles} to fill this row (${total} of ${size})` };
  if (row.perPerson && total < ALIKE_BELOW)
    return { level: "alike", text: "People's rows will be much alike — works best in a shared row" };
  return { level: "ok", text: `Enough ${titles} for this row` };
}
