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

/** What a season's count means for the row it is being added to (#137 D10). Never blocks a save. */
export function seasonVerdict(total: number, row: Pick<SeasonRow, "size" | "perPerson" | "media">): SeasonVerdict {
  const titles = titleNoun(row.media, 2);
  if (total < row.size) return { level: "few", text: `Too few ${titles} to fill this row (${total} of ${row.size})` };
  if (row.perPerson && total < ALIKE_BELOW)
    return { level: "alike", text: "People's rows will be much alike — works best in a shared row" };
  return { level: "ok", text: `Enough ${titles} for this row` };
}
