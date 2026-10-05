/** Web search asks about one watched title at a time, which is not seasonal, so a seasonal row drops
 *  it (`rows.effective_row_sources`). Shared so the editor and the row badges agree on it. */
export function withoutWebSearchWhenSeasonal(
  sources: readonly string[],
  seasons: readonly string[],
): readonly string[] {
  return seasons.length > 0 ? sources.filter((source) => source !== "llm_web") : sources;
}
