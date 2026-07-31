import { describe, expect, it } from "vitest";

import {
  buildLibraries,
  fateLabel,
  mediaGroupLabel,
  mediaLabel,
  sourceRole,
  watchedSummary,
  webMechanism,
} from "@/lib/trace";
import type { RunUserTraceResponse } from "@/lib/types";

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
          picks: [{
            rank: 1,
            title: "X",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
          picks: [{
            rank: 1,
            title: "Y",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
          picks: [{
            rank: 1,
            title: "X",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
          picks: [{
            rank: 1,
            title: "X",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
          picks: [{
            rank: 1,
            title: "Y",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
          picks: [{
            rank: 1,
            title: "Z",
            reason: "",
            media_type: "show",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
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
            picks: [{
            rank: 1,
            title: "X",
            reason: "",
            media_type: "movie",
            seed_title: null,
            sources: [],
            affinity: null,
          }],
          },
        ],
      }),
    )[0];
    expect(movieOnly && watchedSummary(movieOnly)).toBe(
      "Watched 5 movies here",
    );
  });
});
