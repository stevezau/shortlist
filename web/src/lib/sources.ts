import type { Settings } from "@/lib/types";
import { settingString } from "@/lib/format";

/**
 * The candidate sources the engine knows how to run. Shortlist pools every enabled source, keeps
 * only what's already in the library, then the AI re-ranks. Enabled globally in Settings →
 * Recommendations, or overridden per row in the row editor. Mirrors engine `KNOWN_SOURCES`.
 */
export interface SourceInfo {
  id: string;
  label: string;
  desc: string;
  /** Compact name for summaries where the full label won't fit (e.g. a row card). */
  short?: string;
  /** A dependency this source needs before it can run; the toggle is disabled until it's satisfied. */
  requires?: "curator" | "trakt" | "web_search";
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
    id: "llm_library",
    label: "AI — suggests from your library",
    short: "AI from library",
    desc: "Your AI curator reads each person's taste and scans your whole library for matches. Honest note: in our testing it adds the fewest new picks for the most AI cost — the other sources already find most of them. Turn it off first if you want to cut AI cost.",
    requires: "curator",
  },
  {
    id: "trakt",
    label: "Trakt — related titles",
    short: "Trakt",
    desc: "Pulls 'what to watch next' picks from Trakt — often catches titles TMDB's similar list misses.",
    requires: "trakt",
  },
  {
    id: "llm_web",
    label: "AI — web search for what to watch next",
    short: "AI web search",
    desc: "Searches the live web for current, well-reviewed titles, then keeps the ones already in your library. Uses your curator's own web search (Claude, GPT, or Gemini) or an Exa key — choose which under Search backend below.",
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

/** How the llm_web source searches: 'native' | 'exa' | 'auto' (owner-chosen). */
export function webSearchProvider(settings: Settings): string {
  return settingString(settings, "llm_web.search_provider") || "auto";
}

/** Whether the current curator provider can search the web with its OWN tool (Claude/GPT/Gemini). */
export function hasNativeWebSearch(settings: Settings): boolean {
  return NATIVE_WEB_SEARCH_PROVIDERS.includes(
    settingString(settings, "curator.provider"),
  );
}

/** Whether an Exa web-search key is on file (the universal search backend; the only path for Ollama). */
export function hasExa(settings: Settings): boolean {
  return Boolean(settingString(settings, "exa.apikey"));
}

/**
 * Whether the llm_web source can actually search under the chosen backend — the mode decides which
 * capability is required, so the toggle can never claim "on" where it would silently do nothing.
 *
 * EVERY backend needs a real AI curator: even the Exa path only SEARCHES externally, then hands the
 * results to the curator to pick titles from. With no curator (heuristic mode) the engine's own
 * `llm_ready` gate skips the source entirely — so an Exa key alone must NOT un-block the toggle.
 */
export function hasWebSearch(settings: Settings): boolean {
  if (!hasCurator(settings)) return false;
  const mode = webSearchProvider(settings);
  if (mode === "native") return hasNativeWebSearch(settings);
  if (mode === "exa") return hasExa(settings);
  return hasNativeWebSearch(settings) || hasExa(settings); // auto
}

/** The reason a source can't be enabled yet, or null when its dependency is satisfied. */
export function sourceBlockedReason(
  source: SourceInfo,
  settings: Settings,
): string | null {
  if (source.requires === "curator" && !hasCurator(settings))
    return "Needs an AI curator — set one up in Connections first.";
  if (source.requires === "trakt" && !hasTrakt(settings))
    return "Needs a Trakt API key — add it in Connections first.";
  if (source.requires === "web_search" && !hasWebSearch(settings)) {
    // A curator is needed in every mode — it picks the titles from the search results. Report that
    // first, since without it no search backend can help.
    if (!hasCurator(settings))
      return "Needs an AI curator to choose titles from the results — set one up in Connections first.";
    const mode = webSearchProvider(settings);
    if (mode === "exa")
      return "Needs an Exa API key — add it in Connections, or switch the search backend to Auto.";
    if (mode === "native")
      return "Needs Claude, GPT, or Gemini — Ollama can’t web-search. Change your curator, or use the Exa backend.";
    return "Needs Claude, GPT, or Gemini — or an Exa API key in Connections (required for Ollama).";
  }
  return null;
}
