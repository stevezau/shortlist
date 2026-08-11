import type { Settings } from "@/lib/types";
import { settingString } from "@/lib/format";

/**
 * The candidate sources the engine knows how to run. Shortlist pools every enabled source, keeps
 * only what's already in the library, then ranks them in code. Enabled globally in Settings →
 * Finding titles, or overridden per row in the row editor. Mirrors engine `KNOWN_SOURCES`.
 */
export interface SourceInfo {
  id: string;
  label: string;
  desc: string;
  /** Compact name for summaries where the full label won't fit (e.g. a row card). */
  short?: string;
  /** A dependency this source needs before it can run; the toggle is disabled until it's satisfied. */
  requires?: "trakt" | "web_search";
}

/** Curator providers that can search the web themselves (a native web-search tool). Ollama can't. */
const NATIVE_WEB_SEARCH_PROVIDERS = ["anthropic", "openai", "google"];

export const SOURCES: readonly SourceInfo[] = [
  {
    id: "tmdb_similar",
    label: "TMDB — similar titles",
    short: "TMDB similar",
    desc: "The baseline: titles TMDB says are similar to what each person watched.",
  },
  {
    id: "tmdb_discover",
    label: "TMDB — discover by taste",
    short: "TMDB discover",
    desc: "Widens the net to popular, well-rated titles in the genres each person leans toward.",
  },
  {
    id: "trakt",
    label: "Trakt — related titles",
    short: "Trakt",
    desc: "Trakt is a site where people log what they watch. This pulls its “watch this next” picks, which often catch titles TMDB's similar list misses.",
    requires: "trakt",
  },
  {
    id: "llm_web",
    label: "AI — web search for what to watch next",
    short: "AI web search",
    desc: "Searches the live web for well-reviewed titles to watch next, then keeps only the ones already in your library. Claude, GPT and Gemini do the searching themselves; any other provider searches through Exa or your own self-hosted SearXNG. Pick which in Settings → Finding titles.",
    requires: "web_search",
  },
];

/** The compact name for a source id — falls back to the raw id for a source the UI doesn't know. */
export function sourceShortLabel(id: string): string {
  const source = SOURCES.find((s) => s.id === id);
  return source?.short ?? source?.label ?? id;
}

/** Whether an AI curator is configured (needed by curator-dependent sources). */
export function hasCurator(settings: Settings): boolean {
  return !["", "none"].includes(settingString(settings, "curator.provider"));
}

/** Whether a Trakt API key is on file (needed by the Trakt source). */
export function hasTrakt(settings: Settings): boolean {
  return Boolean(settingString(settings, "trakt.client_id"));
}

/** Whether an MDBList API key is on file (needed by every non-TMDB request rating source). */
export function hasMdblist(settings: Settings): boolean {
  return Boolean(settingString(settings, "requests.mdblist.apikey"));
}

/** Which backend the llm_web source searches with: 'native' | 'exa' | 'searxng' (owner-chosen).
 *  There was an 'auto' (native unioned with an external); it was removed in 1.3 and migration 0063
 *  pins every install off it, so a stored 'auto' reads as the default. */
export function webSearchProvider(settings: Settings): string {
  const stored = settingString(settings, "llm_web.search_provider");
  return stored && stored !== "auto" ? stored : "native";
}

/** Whether the current curator provider can search the web with its OWN tool (Claude/GPT/Gemini). */
export function hasNativeWebSearch(settings: Settings): boolean {
  return NATIVE_WEB_SEARCH_PROVIDERS.includes(
    settingString(settings, "curator.provider"),
  );
}

/** Whether an Exa web-search key is on file (a universal search backend — works for any provider). */
export function hasExa(settings: Settings): boolean {
  return Boolean(settingString(settings, "exa.apikey"));
}

/** Whether a self-hosted SearXNG address is on file (the fully-local search backend). */
export function hasSearxng(settings: Settings): boolean {
  return Boolean(settingString(settings, "searxng.url"));
}

/** Whether EITHER external search backend is set up (the two ways a non-native provider searches). */
export function hasExternalSearch(settings: Settings): boolean {
  return hasExa(settings) || hasSearxng(settings);
}

/**
 * Whether the llm_web source can actually search under the chosen backend — the mode decides which
 * capability is required, so the toggle can never claim "on" where it would silently do nothing.
 *
 * EVERY backend needs a real AI provider: even the external paths only SEARCH, then hand the results
 * to the provider to pick titles from. With no provider (heuristic mode) the engine's own
 * `llm_ready` gate skips the source entirely — so a search backend alone must NOT un-block the
 * toggle. Mirrors `candidates._web_search_capable` on the server.
 */
export function hasWebSearch(settings: Settings): boolean {
  if (!hasCurator(settings)) return false;
  const mode = webSearchProvider(settings);
  if (mode === "exa") return hasExa(settings);
  if (mode === "searxng") return hasSearxng(settings);
  return hasNativeWebSearch(settings); // native
}

/** The reason a source can't be enabled yet, or null when its dependency is satisfied. */
export function sourceBlockedReason(
  source: SourceInfo,
  settings: Settings,
): string | null {
  if (source.requires === "trakt" && !hasTrakt(settings))
    return "Needs a Trakt API key — add it in Connections first.";
  if (source.requires === "web_search" && !hasWebSearch(settings)) {
    // An AI provider is needed in every mode — it picks the titles from the search results. Report
    // that first, since without it no search backend can help.
    if (!hasCurator(settings))
      return "Needs an AI provider to choose titles from the results — set one up in Connections first.";
    const mode = webSearchProvider(settings);
    if (mode === "exa")
      return "Needs an Exa API key — add it in Connections, or switch the search backend to Auto.";
    if (mode === "searxng")
      return "Needs the address of your SearXNG instance — add it in Connections, or switch the search backend to Auto.";
    return "Needs Claude, GPT, or Gemini — Ollama can't web-search. Change your AI provider in Connections, or switch the search backend there to Exa or your own SearXNG.";
  }
  return null;
}
