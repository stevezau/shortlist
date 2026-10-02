/** Below this many films, per-person rows of one season come out much alike: #124 judged 63 too few to
 *  make them differ. A heuristic, not a measured line (#137 D10). */
export const ALIKE_BELOW = 100;

export type SeasonVerdict = { level: "few" | "alike" | "ok"; text: string };

/** What a season's film count means for the row it is being added to (#137 D10). Never blocks a save. */
export function seasonVerdict(total: number, rowSize: number, perPerson: boolean): SeasonVerdict {
  if (total < rowSize) return { level: "few", text: `Too few to fill this row (${total} of ${rowSize})` };
  if (perPerson && total < ALIKE_BELOW)
    return { level: "alike", text: "People's rows will be much alike — works best in a shared row" };
  return { level: "ok", text: "Enough for this row" };
}
