import { hasCurator, webSearchProvider } from "@/lib/sources";
import type { Settings } from "@/lib/types";

/** The presets only set the two history-mix counts; `recent_count` stays its own field. */
export const MIX_PRESETS = [
  { id: "recent_only", label: "Recent only", favourites: 0, older: 0 },
  { id: "little", label: "A little older", favourites: 3, older: 3 },
  { id: "balanced", label: "Balanced", favourites: 6, older: 6 },
  { id: "deep", label: "Deep", favourites: 10, older: 10 },
] as const;

export const MIX_MAX = 10;

/** The preset whose two counts these are, or null when they match none (the Custom case). */
export function presetFor(favourites: number, older: number) {
  return MIX_PRESETS.find((p) => p.favourites === favourites && p.older === older) ?? null;
}

/** "Balanced", or "Custom" when the counts match no preset. */
export function mixName(favourites: number, older: number): string {
  return presetFor(favourites, older)?.label ?? "Custom";
}

/** What one favourite or older watch costs, in the words of the configured web-search setup.
 *  Null while settings are loading, so nothing is claimed. */
export function mixModeLine(settings: Settings | undefined): string | null {
  if (!settings) return null;
  const provider = webSearchProvider(settings);
  if (provider === "exa") {
    return hasCurator(settings)
      ? "Each one is one more Exa search, cached a week, and the AI is asked to give each group its fair share of picks."
      : "Each one is one more Exa search, cached a week.";
  }
  if (provider === "searxng") return "Each one is one more search on your SearXNG server.";
  return "Favourites and older watches are added to what the AI is told; Shortlist runs no extra searches.";
}
