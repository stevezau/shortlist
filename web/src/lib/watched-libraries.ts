import type { WatchedPage } from "./types";

/** Which libraries the filter should offer — and, by returning fewer than two, whether it should
 *  appear at all.
 *
 *  A library dropdown is only worth its space when a library choice says something the Movies/Shows
 *  buttons beside it cannot. On the common server — one "Movies" library, one "TV Shows" library —
 *  it offers those same two words a second time, so it is not shown. It appears when one TYPE holds
 *  more than one library ("Movies" + "4K Movies"), which is the server shape issue #111 came from.
 *
 *  With a type selected, only that type's libraries are offered: "4K Movies" under a Shows filter
 *  can only ever return nothing. The currently selected library is always kept in the list, so a
 *  library renamed in Plex between requests can still be seen and cleared rather than leaving an
 *  invisible filter applied.
 */
export function libraryOptions(
  libraries: WatchedPage["libraries"],
  mediaType: "" | "movie" | "show",
  selected: string,
): string[] {
  const perType = librariesPerType(libraries);
  const disambiguates = [...perType.values()].some((names) => names.size > 1);
  if (!disambiguates && !selected) return [];
  const offered = mediaType
    ? (perType.get(mediaType) ?? new Set<string>())
    : new Set(libraries.map((entry) => entry.name));
  return [...new Set([...offered, ...(selected ? [selected] : [])])].sort();
}

function librariesPerType(
  libraries: WatchedPage["libraries"],
): Map<string, Set<string>> {
  const perType = new Map<string, Set<string>>();
  for (const entry of libraries) {
    if (!perType.has(entry.media_type)) perType.set(entry.media_type, new Set());
    perType.get(entry.media_type)?.add(entry.name);
  }
  return perType;
}
