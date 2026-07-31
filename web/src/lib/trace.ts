/** The per-library view model for "How we picked" (`pages/run-user-trace.tsx`), and the pure
 *  transform that builds it.
 *
 *  The engine trace is keyed by stage (history/seeds/gathers) with a library NAME on each watch and
 *  seed, and its search sources work per media TYPE (movies vs shows), not per named library. So a
 *  library tab is assembled here: its watches/seeds by name, the sources for its media type, and the
 *  delivered picks whose breakdown targets it. */
import type {
  RunLibraryBreakdown,
  RunUserTrace,
  RunUserTraceResponse,
  TraceFate,
  TraceSeed,
  TraceSource,
  TraceWatch,
  TraceWeb,
} from "@/lib/types";

export interface LibraryView {
  key: string;
  label: string;
  /** movie | show | both — how this library's candidate SEARCH is scoped (search is per-type). */
  media: string;
  /** The most-recent watches recorded (a bounded sample — the true totals are the counts below). */
  watched: TraceWatch[];
  /** True distinct-title watch totals for this tab's media type(s), NOT the sample length: the
   *  recent sample is time-ordered and can be all TV, so a movie tab may sample only a handful. */
  watchedMovies: number;
  watchedShows: number;
  seeds: TraceSeed[];
  /** Every source EXCEPT llm_web, which is rendered on its own from `web`/`webSource` (it has a
   *  richer story than a contribution count — the web searches and the AI's proposals). */
  sources: TraceSource[];
  web: TraceWeb | null;
  /** The llm_web source row (its status/contribution), paired with `web` in the AI-search card. */
  webSource: TraceSource | null;
  discoverGenres: Record<string, string[]>;
  delivered: RunLibraryBreakdown[];
  /** True when search is shared with other libraries of the same media type (be honest about it). */
  sharedSearch: boolean;
}

/** A gather pool is labelled "{media} · {sources}"; it covers a library if its media part overlaps
 *  the library's media types (or is "both"). Blank/legacy pools are treated as covering everything. */
function poolCoversMedia(pool: string, medias: Set<string>): boolean {
  const media = pool.split(" · ")[0]?.trim() ?? "";
  if (!media || media === "both" || medias.size === 0) return true;
  return medias.has(media);
}

/** Union the same source across gathers into one row, keeping only the per-return rows whose media
 *  belongs to this library, and re-tallying disposition from those rows so the counts match. */
function mergeSourcesForMedia(
  sources: TraceSource[],
  medias: Set<string>,
): TraceSource[] {
  const byName = new Map<string, TraceSource>();
  for (const src of sources) {
    const queries = (src.queries ?? [])
      .filter((q) => medias.size === 0 || medias.has(q.media))
      .map((q) => ({ ...q }));
    const existing = byName.get(src.source);
    if (existing) {
      existing.queries = [...(existing.queries ?? []), ...queries];
      existing.contributed += src.contributed;
    } else {
      byName.set(src.source, { ...src, queries });
    }
  }
  // Re-tally disposition from the (possibly filtered) queries so per-tab counts are truthful.
  for (const src of byName.values()) {
    const tally: Record<string, number> = {};
    for (const q of src.queries ?? []) {
      for (const r of q.returned)
        if (r.fate) tally[r.fate] = (tally[r.fate] ?? 0) + 1;
    }
    if (Object.keys(tally).length > 0) src.disposition = tally;
  }
  return [...byName.values()];
}

export function buildLibraries(data: RunUserTraceResponse): LibraryView[] {
  const trace: RunUserTrace = data.trace ?? {};
  const watches = trace.history?.recent ?? [];
  // The true distinct-title totals across ALL history, not the length of the recent sample. The
  // sample is time-ordered and bounded, so a heavy TV watcher's movie tab sees only a few recent
  // movie titles — these are what "watched N movies / M shows" should actually report.
  const totalMovies = trace.history?.watched_movies ?? 0;
  const totalShows = trace.history?.watched_shows ?? 0;
  // Exact per-library totals (split by media) when the run recorded them — this distinguishes two
  // libraries of the same media type, which the server-wide per-media total above cannot.
  const byLibrary = trace.history?.watched_by_library;
  const seeds = trace.seeds ?? [];
  const gathers = trace.gathers ?? [];
  const breakdown = data.breakdown ?? [];

  // Every library name we know about, in a stable first-seen order: delivered rows first (the
  // outcome), then any library that only shows up in watches/seeds.
  const order: string[] = [];
  const seen = new Set<string>();
  const remember = (label: string) => {
    if (label && !seen.has(label)) {
      seen.add(label);
      order.push(label);
    }
  };
  for (const b of breakdown) remember(b.library_title);
  for (const w of watches) remember(w.library || mediaGroupLabel(w.media));
  for (const s of seeds) remember(s.library || mediaGroupLabel(s.media));

  // Which media type each library holds — inferred from its own watches/seeds, so we can attach the
  // right (per-type) search sources. A library with only movie watches is a movie library.
  const mediaOf = new Map<string, Set<string>>();
  const note = (label: string, media: string) => {
    if (!label) return;
    const set = mediaOf.get(label) ?? new Set<string>();
    set.add(media);
    mediaOf.set(label, set);
  };
  for (const w of watches) note(w.library || mediaGroupLabel(w.media), w.media);
  for (const s of seeds) note(s.library || mediaGroupLabel(s.media), s.media);
  for (const b of breakdown) {
    for (const p of b.picks) note(b.library_title, p.media_type ?? "");
  }

  // How many named libraries share each media type — for the honest "search shared across your movie
  // libraries" note.
  const libsPerMedia = new Map<string, number>();
  for (const [, medias] of mediaOf) {
    for (const m of medias) libsPerMedia.set(m, (libsPerMedia.get(m) ?? 0) + 1);
  }

  return order.map((label) => {
    const medias = mediaOf.get(label) ?? new Set<string>();
    const primaryMedia =
      medias.size === 1 ? ([...medias][0] ?? "both") : "both";
    const libWatches = watches.filter(
      (w) => (w.library || mediaGroupLabel(w.media)) === label,
    );
    const libSeeds = seeds.filter(
      (s) => (s.library || mediaGroupLabel(s.media)) === label,
    );
    // A gather is relevant to this library if its pool covers this library's media type. Union the
    // sources across those gathers, keeping only the per-return rows for this library's media.
    const relevant = gathers.filter((g) => poolCoversMedia(g.pool, medias));
    const merged = mergeSourcesForMedia(
      relevant.flatMap((g) => g.sources ?? []),
      medias,
    );
    // llm_web is pulled OUT of the generic source list: it has its own rich card (the web searches
    // it ran + the titles the AI proposed), so showing it twice — once as a bare "Contributed N"
    // row and once as the detailed card — is the confusing duplication we're removing.
    const sources = merged.filter((s) => s.source !== "llm_web");
    const webSource = merged.find((s) => s.source === "llm_web") ?? null;
    const web = relevant.map((g) => g.web).find(Boolean) ?? null;
    const discoverGenres: Record<string, string[]> = {};
    for (const g of relevant) {
      for (const [m, names] of Object.entries(g.discover_genres ?? {})) {
        if (medias.size === 0 || medias.has(m)) discoverGenres[m] = names;
      }
    }
    const sharedSearch = [...medias].some(
      (m) => (libsPerMedia.get(m) ?? 0) > 1,
    );
    // The true watched totals belong to this tab's media type(s). A movie-only tab reports the
    // movie total; a "both" tab reports both. These come from the full-history counts, not the
    // bounded recent sample, so a TV-heavy watcher's movie tab no longer reads "4 watched". Prefer
    // the exact per-library split (which distinguishes two same-type libraries); fall back to the
    // server-wide per-media total for runs recorded before per-library totals existed.
    const libTotals = byLibrary?.[label];
    const watchedMovies = libTotals
      ? libTotals.movie
      : medias.size === 0 || medias.has("movie")
        ? totalMovies
        : 0;
    const watchedShows = libTotals
      ? libTotals.show
      : medias.size === 0 || medias.has("show")
        ? totalShows
        : 0;
    return {
      key: label,
      label,
      media: primaryMedia,
      watched: libWatches,
      watchedMovies,
      watchedShows,
      seeds: libSeeds,
      sources,
      web,
      webSource,
      discoverGenres,
      delivered: breakdown.filter((b) => b.library_title === label),
      sharedSearch,
    };
  });
}

// ── Plain-English helpers ─────────────────────────────────────────────────────

/** One sentence saying what we sent a source and what it does — so "TMDB (your genres) · 40 · 4 kept
 * · 4 dropped" reads as a story, not a code. Each source is fed something different: TMDB-similar and
 * Trakt take each recent watch and return look-alikes; TMDB-discover takes the person's top genres
 * (not a title) and returns what's popular in them. */
export function sourceRole(source: string): string {
  switch (source) {
    case "tmdb_similar":
      return "We asked TMDB for titles similar to each recent watch above.";
    case "tmdb_discover":
      return "We asked TMDB for what's popular in the genres they watch most.";
    case "trakt":
      return "We asked Trakt for titles people who watched the same films also watched.";
    case "cold_start":
      return "With little history to go on, we pulled the highest-rated titles on this server.";
    default:
      return "We gathered candidate titles from this source.";
  }
}

/** Plain-English description of HOW the AI web search ran, from the mode + whether Exa searches were
 * recorded. The three engine modes (candidates.py): native = the model's own built-in web search;
 * exa = the external Exa search, then the model ranks; auto = both, unioned. */
export function webMechanism(mode: string, hasSearches: boolean): string {
  if (mode === "native")
    return "The AI model’s own built-in web search proposed titles directly.";
  if (mode === "exa" || (hasSearches && mode !== "auto"))
    return "We searched the web with Exa, then the AI read the results and proposed titles.";
  if (mode === "auto")
    return hasSearches
      ? "The AI model’s built-in web search AND an Exa web search, combined — the AI proposed titles from both."
      : "The AI model’s own built-in web search proposed titles directly.";
  return "The AI proposed titles to watch next from a web search.";
}

/** "Watched 598 movies and 40 shows" from the true totals — so a tab whose recent sample is all TV
 *  doesn't read as "4 watched". Only names the media type(s) this tab actually holds. */
export function watchedSummary(lib: LibraryView): string {
  const parts: string[] = [];
  if (lib.watchedMovies > 0)
    parts.push(
      `${lib.watchedMovies.toLocaleString()} movie${lib.watchedMovies === 1 ? "" : "s"}`,
    );
  if (lib.watchedShows > 0)
    parts.push(
      `${lib.watchedShows.toLocaleString()} show${lib.watchedShows === 1 ? "" : "s"}`,
    );
  return parts.length > 0 ? `Watched ${parts.join(" and ")} here` : "";
}

/** "movie" → "Movie", "show" → "Show", "both" → "Movies & shows". */
export function mediaLabel(media: string): string {
  if (media === "movie") return "Movie";
  if (media === "show") return "Show";
  if (media === "both") return "Movies & shows";
  return media;
}

/** Media-type heading when a real library name is unknown (legacy runs). Never wins over a name. */
export function mediaGroupLabel(media: string): string {
  if (media === "movie") return "Movies";
  if (media === "show") return "TV Shows";
  return media || "Other";
}

/** Why a returned title didn't make the shortlist, in plain words. */
export function fateLabel(fate: TraceFate): string {
  switch (fate) {
    case "already_watched":
      return "already watched";
    case "not_in_your_libraries":
      return "not in your libraries";
    case "excluded_genre":
      return "excluded genre";
    case "lost_ranking_cutoff":
      return "lost the ranking cut";
    case "not_returned":
      return "found by another source";
    default:
      return "";
  }
}
