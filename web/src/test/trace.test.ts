import { describe, expect, it } from "vitest";

import {
  buildLibraries,
  fateLabel,
  mediaGroupLabel,
  mediaLabel,
  sourceRole,
  watchedSummary,
  orderingRows,
  requestNote,
  shortlistBreakdown,
  webMechanism,
} from "@/lib/trace";
import type { Pick, RunUserTraceResponse } from "@/lib/types";
import type { LibraryView } from "@/lib/trace";

function trace(
  patch: Partial<RunUserTraceResponse> = {},
): RunUserTraceResponse {
  return {
    username: "sarah",
    display_name: "Sarah",
    status: "ok",
    error: null,
    reason: null,
    trace: {},
    breakdown: [],
    requests: {},
    ...patch,
  };
}

describe("buildLibraries", () => {
  it("orders libraries by first-seen, delivered rows before watch/seed-only libraries", () => {
    const data = trace({
      trace: {
        history: {
          total: 2,
          watched_movies: 1,
          watched_shows: 0,
          recent: [
            {
              title: "A",
              media: "movie",
              library: "Watched Only",
              year: null,
              watched_at: null,
            },
          ],
        },
      },
      breakdown: [
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "1",
          library_title: "Delivered Lib",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [],
        },
      ],
    });
    const libs = buildLibraries(data);
    expect(libs.map((l) => l.key)).toEqual(["Delivered Lib", "Watched Only"]);
  });

  it("falls back to the server-wide per-media total, but prefers an exact per-library split", () => {
    const data = trace({
      trace: {
        history: {
          total: 10,
          recent: [],
          watched_movies: 8,
          watched_shows: 2,
          watched_by_library: { "4K Movies": { movie: 3, show: 0 } },
        },
      },
      breakdown: [
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "1",
          library_title: "Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "X",
              reason: "",
              media_type: "movie",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "2",
          library_title: "4K Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Y",
              reason: "",
              media_type: "movie",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
      ],
    });
    const libs = buildLibraries(data);
    // No exact split recorded for "Movies" -> falls back to the server-wide movie total.
    expect(libs.find((l) => l.key === "Movies")?.watchedMovies).toBe(8);
    // "4K Movies" has its own exact split, which must win over the shared total.
    expect(libs.find((l) => l.key === "4K Movies")?.watchedMovies).toBe(3);
  });

  it("pulls llm_web out of the generic source list into its own webSource slot", () => {
    const data = trace({
      trace: {
        gathers: [
          {
            pool: "movie · tmdb_similar",
            sources: [
              {
                source: "tmdb_similar",
                status: "ok",
                contributed: 2,
                detail: "",
              },
              { source: "llm_web", status: "ok", contributed: 1, detail: "" },
            ],
          },
        ],
      },
      breakdown: [
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "1",
          library_title: "Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "X",
              reason: "",
              media_type: "movie",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
      ],
    });
    const lib = buildLibraries(data)[0];
    expect(lib?.sources.map((s) => s.source)).toEqual(["tmdb_similar"]);
    expect(lib?.webSource?.source).toBe("llm_web");
  });

  it("flags sharedSearch only when >1 named library holds the same media type", () => {
    const data = trace({
      breakdown: [
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "1",
          library_title: "Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "X",
              reason: "",
              media_type: "movie",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "2",
          library_title: "4K Movies",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Y",
              reason: "",
              media_type: "movie",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
        {
          row_slug: "picked",
          row_title: "Picked",
          library_key: "3",
          library_title: "TV",
          added: [],
          removed: [],
          kept: [],
          deleted: [],
          created: true,
          picks: [
            {
              rank: 1,
              title: "Z",
              reason: "",
              media_type: "show",
              seed_title: null,
              sources: [],
              affinity: null,
            },
          ],
        },
      ],
    });
    const libs = buildLibraries(data);
    expect(libs.find((l) => l.key === "Movies")?.sharedSearch).toBe(true);
    expect(libs.find((l) => l.key === "4K Movies")?.sharedSearch).toBe(true);
    expect(libs.find((l) => l.key === "TV")?.sharedSearch).toBe(false);
  });

  it("returns an empty list for a run with no stages and no breakdown", () => {
    expect(buildLibraries(trace())).toEqual([]);
  });
});

describe("plain-English trace helpers", () => {
  it("mediaLabel names each media type, and passes through anything unrecognised", () => {
    expect(mediaLabel("movie")).toBe("Movie");
    expect(mediaLabel("show")).toBe("Show");
    expect(mediaLabel("both")).toBe("Movies & shows");
    expect(mediaLabel("weird")).toBe("weird");
  });

  it("mediaGroupLabel is the legacy-run heading, defaulting to 'Other'", () => {
    expect(mediaGroupLabel("movie")).toBe("Movies");
    expect(mediaGroupLabel("show")).toBe("TV Shows");
    expect(mediaGroupLabel("")).toBe("Other");
  });

  it("fateLabel explains every recognised drop reason, and nothing for an unknown one", () => {
    expect(fateLabel("already_watched")).toBe("already watched");
    expect(fateLabel("not_in_your_libraries")).toBe("not in your libraries");
    expect(fateLabel("excluded_genre")).toBe("excluded genre");
    expect(fateLabel("lost_ranking_cutoff")).toBe("lost the ranking cut");
    expect(fateLabel("not_returned")).toBe("found by another source");
  });

  it("sourceRole describes each source's real query shape", () => {
    expect(sourceRole("tmdb_discover")).toMatch(/genres they watch most/);
    expect(sourceRole("cold_start")).toMatch(/highest-rated titles/);
    expect(sourceRole("unknown_source")).toMatch(/gathered candidate titles/);
  });

  it("webMechanism distinguishes native, Exa, and auto (with and without searches)", () => {
    expect(webMechanism("native", false)).toMatch(/built-in web search/);
    expect(webMechanism("exa", true)).toMatch(/searched the web with Exa/);
    expect(webMechanism("auto", true)).toMatch(/AND an Exa web search/);
    expect(webMechanism("auto", false)).toMatch(
      /built-in web search proposed titles directly/,
    );
  });

  it("webMechanism names SearXNG when that is the backend that ran", () => {
    expect(webMechanism("searxng", true)).toMatch(
      /searched the web with SearXNG/,
    );
  });

  it("webMechanism trusts the recorded provider over the mode under auto", () => {
    // `auto` means "native plus whichever external is configured" — the mode alone cannot say which
    // one ran, so the run records it. Without this the trace would claim Exa on a SearXNG-only server.
    expect(webMechanism("auto", true, "searxng")).toMatch(/SearXNG/);
    expect(webMechanism("auto", true, "exa")).toMatch(/Exa/);
  });

  it("webMechanism falls back to Exa's wording for runs recorded before the provider was traced", () => {
    expect(webMechanism("auto", true, undefined)).toMatch(/Exa/);
  });

  it("watchedSummary names only the media type(s) a library actually holds", () => {
    const movieOnly = buildLibraries(
      trace({
        trace: {
          history: {
            total: 5,
            recent: [],
            watched_movies: 5,
            watched_shows: 0,
          },
        },
        breakdown: [
          {
            row_slug: "picked",
            row_title: "Picked",
            library_key: "1",
            library_title: "Movies",
            added: [],
            removed: [],
            kept: [],
            deleted: [],
            created: true,
            picks: [
              {
                rank: 1,
                title: "X",
                reason: "",
                media_type: "movie",
                seed_title: null,
                sources: [],
                affinity: null,
              },
            ],
          },
        ],
      }),
    )[0];
    expect(movieOnly && watchedSummary(movieOnly)).toBe(
      "Watched 5 movies here",
    );
  });
});

describe("shortlistBreakdown — the per-title answer to 'what survived and why'", () => {
  const lib = (sources: unknown[]) =>
    ({ sources, webSource: null }) as unknown as LibraryView;

  const src = (name: string, returned: unknown[], media = "movie", total?: number) => ({
    source: name,
    status: "ok",
    contributed: 0,
    detail: "",
    queries: [{ seed: "S", media, returned, total: total ?? returned.length }],
  });

  it("groups every title by what happened to it", () => {
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [
          { tmdb_id: 1, title: "Kept One", fate: "kept", year: 2024, rating: 8, age_weight: 1 },
          { tmdb_id: 2, title: "Cut One", fate: "lost_ranking_cutoff", year: 1999, rating: 9, age_weight: 0.3 },
          { tmdb_id: 3, title: "Seen It", fate: "already_watched", year: 2020, rating: 7 },
        ]),
      ]),
    );
    expect(out.total).toBe(3);
    expect(out.groups.find((g) => g.fate === "kept")?.titles.map((t) => t.title)).toEqual(["Kept One"]);
    expect(out.groups.find((g) => g.fate === "lost_ranking_cutoff")?.titles[0]?.age_weight).toBe(0.3);
  });

  it("counts a title found by two sources once", () => {
    // The pool dedupes by (tmdb_id, media); counting per source would inflate every number on
    // screen and make the funnel not add up.
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [{ tmdb_id: 1, title: "Both", fate: "kept", year: 2024, rating: 8 }]),
        src("trakt", [{ tmdb_id: 1, title: "Both", fate: "kept", year: 2024, rating: 8 }]),
      ]),
    );
    expect(out.total).toBe(1);
  });

  it("orders the cut list by the release-date weight that decided it", () => {
    // The point of the list: "why did a 2003 title beat a 2024 one" is answered by this number.
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [
          { tmdb_id: 1, title: "Old", fate: "lost_ranking_cutoff", year: 1990, rating: 9, age_weight: 0.1 },
          { tmdb_id: 2, title: "Newer", fate: "lost_ranking_cutoff", year: 2022, rating: 7, age_weight: 0.9 },
        ]),
      ]),
    );
    const cut = out.groups.find((g) => g.fate === "lost_ranking_cutoff");
    expect(cut!.titles.map((t) => t.title)).toEqual(["Newer", "Old"]);
  });

  it("puts what survived first, then the biggest removal reason", () => {
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [
          { tmdb_id: 1, title: "A", fate: "already_watched" },
          { tmdb_id: 2, title: "B", fate: "lost_ranking_cutoff" },
          { tmdb_id: 3, title: "C", fate: "lost_ranking_cutoff" },
          { tmdb_id: 4, title: "D", fate: "kept" },
        ]),
      ]),
    );
    expect(out.groups.map((g) => g.fate)).toEqual([
      "kept",
      "lost_ranking_cutoff",
      "already_watched",
    ]);
  });

  it("keeps a movie and a show that share a tmdb id apart", () => {
    // The pool dedupes by (tmdb_id, media) — the DB constraint is the pair — so keying on the id
    // alone silently swallowed one of them and made the total disagree with the funnel.
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [{ tmdb_id: 1396, title: "Movie 1396", fate: "kept" }], "movie"),
        src("trakt", [{ tmdb_id: 1396, title: "Show 1396", fate: "kept" }], "show"),
      ]),
    );
    expect(out.total).toBe(2);
  });

  it("flags that the view is partial when a source's returns were capped", () => {
    // `returned` is a capped display sample. Without the flag, "all N candidates" makes an
    // unrecorded title read as one that was never a candidate.
    const out = shortlistBreakdown(
      lib([src("tmdb_similar", [{ tmdb_id: 1, title: "Sampled", fate: "kept" }], "movie", 40)]),
    );
    expect(out.total).toBe(1);
    expect(out.sampled).toBe(true);
  });

  it("does NOT claim sampling when everything returned was recorded", () => {
    // The number this replaced ("N of M") summed per-seed totals, so two seeds agreeing inflated M
    // and the disclaimer fired on runs where nothing had been withheld at all.
    const out = shortlistBreakdown(
      lib([
        src("tmdb_similar", [{ tmdb_id: 1, title: "A", fate: "kept" }], "movie", 1),
        src("trakt", [{ tmdb_id: 1, title: "A", fate: "kept" }], "movie", 1),
      ]),
    );
    expect(out.total).toBe(1);
    expect(out.sampled).toBe(false);
  });

  it("is empty for a legacy run that recorded no fates", () => {
    const out = shortlistBreakdown(lib([src("tmdb_similar", [{ tmdb_id: 1, title: "No fate" }])]));
    expect(out.total).toBe(0);
    expect(out.groups).toEqual([]);
  });
});

describe("orderingRows — making the fairness passes visible", () => {
  const pick = (rank: number, source: string, seed: string) =>
    ({
      rank,
      title: `T${rank}`,
      reason: "",
      media_type: "movie",
      seed_title: seed,
      sources: [source],
      affinity: 0.5,
    }) as unknown as Pick;

  it("marks where the source's turn changes, which is the round-robin you can otherwise only take on trust", () => {
    const rows = orderingRows([
      pick(1, "tmdb_similar", "A"),
      pick(2, "trakt", "A"),
      pick(3, "tmdb_similar", "A"),
    ]);
    expect(rows.map((r) => r.newSource)).toEqual([true, true, true]);
  });

  it("does not mark a repeat of the same source", () => {
    const rows = orderingRows([
      pick(1, "tmdb_similar", "A"),
      pick(2, "tmdb_similar", "A"),
    ]);
    expect(rows.map((r) => r.newSource)).toEqual([true, false]);
  });

  it("marks where the seed's turn changes — the second fairness pass", () => {
    const rows = orderingRows([
      pick(1, "tmdb_similar", "Dune"),
      pick(2, "tmdb_similar", "Dune"),
      pick(3, "tmdb_similar", "Arrival"),
    ]);
    expect(rows.map((r) => r.newSeed)).toEqual([true, false, true]);
  });

  it("keeps rank order even if the input is not sorted", () => {
    const rows = orderingRows([pick(3, "a", "X"), pick(1, "b", "Y"), pick(2, "c", "Z")]);
    expect(rows.map((r) => r.pick.rank)).toEqual([1, 2, 3]);
  });

  it("is empty when nothing was delivered", () => {
    expect(orderingRows([])).toEqual([]);
  });
});

describe("requestNote — what became of a wanted-but-missing title", () => {
  it("says it went to the Arr, naming which", () => {
    expect(requestNote({ status: "sent", detail: "added to Sonarr and searching", excluded: false, arr_slug: null }))
      .toBe("requested — added to Sonarr and searching");
  });

  it("gives the REASON a queued title is still waiting", () => {
    // The whole point: "pending" alone never answered "so why didn't this one go?".
    expect(
      requestNote({
        status: "pending",
        detail: "rating below auto_min_rating (7.5)",
        excluded: false,
        arr_slug: null,
      }),
    ).toBe("waiting for approval — rating below auto_min_rating (7.5)");
  });

  it("falls back gracefully when an older run recorded no reason", () => {
    expect(requestNote({ status: "pending", detail: "", excluded: false, arr_slug: null })).toBe(
      "waiting for approval",
    );
  });

  it("calls out an exclusion on a title that is still waiting", () => {
    expect(
      requestNote({ status: "pending", detail: "", excluded: true, arr_slug: null }),
    ).toBe("waiting — on an Arr exclusion list");
  });

  it("reports a title the owner rejected", () => {
    expect(
      requestNote({ status: "rejected", detail: "", excluded: false, arr_slug: null }),
    ).toBe("not requested");
  });

  it("does not call a SENT title excluded", () => {
    // `excluded` is never cleared when a title is later sent by hand, so testing it first made one
    // title read "requested" in one place on the page and "not requested" a few lines away.
    expect(
      requestNote({ status: "sent", detail: "added to Radarr and searching", excluded: true, arr_slug: null }),
    ).toBe("requested — added to Radarr and searching");
  });

  it("is null for a title the request pass never considered", () => {
    expect(requestNote(undefined)).toBeNull();
  });
});
