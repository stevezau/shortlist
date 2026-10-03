import { describe, expect, it } from "vitest";

import { hasUnsavedChanges, rowOverrides, toInput } from "@/lib/collections";
import type { Collection, PlexLibrary } from "@/lib/types";

const LIBRARIES: PlexLibrary[] = [
  { key: "1", title: "Movies", type: "movie" },
  { key: "2", title: "4K Movies", type: "movie" },
  { key: "3", title: "TV Shows", type: "show" },
];

function collection(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    refresh_days: null,
    placement: "both",
    placement_friends: "both",
    pin_top: false,
    hub_anchor: {},
    ...patch,
  } as Collection;
}

describe("hasUnsavedChanges", () => {
  it("reports a form matching the saved row as clean", () => {
    const row = collection();
    expect(hasUnsavedChanges(toInput(row), row)).toBe(false);
  });

  it("reports an edited field as unsaved", () => {
    const row = collection();
    expect(hasUnsavedChanges({ ...toInput(row), size: 25 }, row)).toBe(true);
  });

  it("compares nested objects and arrays by VALUE, not by identity or key order", () => {
    // The reason this is not `JSON.stringify`. The form rebuilds `poster` and `hub_anchor`
    // wholesale as you edit, so a stringify comparison reports a row as edited for having been
    // looked at — a warning about nothing, next to the Run button.
    const row = collection({
      library_keys: ["1", "2"],
      hub_anchor: {
        "1": { anchor: "recentlyAdded", row: "", before: true, top: false },
      },
    });
    const saved = toInput(row);
    const reordered = {
      ...saved,
      library_keys: [...saved.library_keys],
      hub_anchor: {
        "1": { top: false, before: true, row: "", anchor: "recentlyAdded" },
      },
      poster: {
        style: saved.poster.style,
        title: saved.poster.title,
        subtitle: saved.poster.subtitle,
        mode: saved.poster.mode,
      },
    };
    expect(hasUnsavedChanges(reordered, row)).toBe(false);

    // ...but a genuinely different nested value is still caught.
    expect(
      hasUnsavedChanges(
        {
          ...reordered,
          hub_anchor: {
            "1": { top: true, before: true, anchor: "recentlyAdded" },
          },
        },
        row,
      ),
    ).toBe(true);
    expect(
      hasUnsavedChanges({ ...reordered, library_keys: ["2", "1"] }, row),
    ).toBe(true);
  });

  it("compares AI instructions as the server stores them: no text on the default, trimmed otherwise", () => {
    const onDefault = collection({ ai_instructions: { mode: "default", text: "" } });
    // Text typed under Add and kept in the draft after switching back is never saved.
    expect(
      hasUnsavedChanges({ ...toInput(onDefault), ai_instructions: { mode: "default", text: "x" } }, onDefault),
    ).toBe(false);
    expect(
      hasUnsavedChanges({ ...toInput(onDefault), ai_instructions: { mode: "add", text: "x" } }, onDefault),
    ).toBe(true);

    const adding = collection({ ai_instructions: { mode: "add", text: "x" } });
    expect(
      hasUnsavedChanges({ ...toInput(adding), ai_instructions: { mode: "add", text: "y" } }, adding),
    ).toBe(true);
    expect(
      hasUnsavedChanges({ ...toInput(adding), ai_instructions: { mode: "own", text: "x" } }, adding),
    ).toBe(true);
    expect(
      hasUnsavedChanges({ ...toInput(adding), ai_instructions: { mode: "add", text: " x " } }, adding),
    ).toBe(false);
  });

  it("treats a row being created as having nothing to differ from", () => {
    expect(hasUnsavedChanges(toInput(collection()), null)).toBe(false);
  });
});

describe("toInput", () => {
  it("carries a row's AI instructions as just the mode and text the API accepts", () => {
    const saved = collection({
      ai_instructions: { mode: "own", text: "Any decade.", extra: 1 },
    });
    expect(toInput(saved).ai_instructions).toEqual({ mode: "own", text: "Any decade." });
  });

  it("reads a row saved without AI instructions as using the default", () => {
    expect(toInput(collection()).ai_instructions).toEqual({ mode: "default", text: "" });
  });
});

describe("rowOverrides", () => {
  it("returns nothing for a row that is entirely on the global defaults", () => {
    expect(rowOverrides(collection(), LIBRARIES)).toEqual([]);
  });

  it("names the row's own sources by their short labels", () => {
    const parts = rowOverrides(
      collection({ candidate_sources: ["trakt", "llm_web"] }),
      LIBRARIES,
    );
    expect(parts).toContain("Sources: Trakt, AI web search");
  });

  it("badges a source whose global dependency isn't met as 'Needs setup', not as active", () => {
    // With settings known: Trakt has a key (runnable), AI web search has neither curator nor Exa key.
    const parts = rowOverrides(
      collection({ candidate_sources: ["trakt", "llm_web"] }),
      LIBRARIES,
      { "trakt.client_id": "•••••" },
    );
    expect(parts).toContain("Sources: Trakt"); // only the runnable one is advertised as active
    expect(parts).toContain("Needs setup: AI web search"); // the dead one is flagged, never claimed
  });

  it("badges a row's own AI instructions, but not one on the default", () => {
    expect(
      rowOverrides(collection({ ai_instructions: { mode: "add", text: "x" } }), LIBRARIES),
    ).toContain("AI instructions: adds to the default");
    expect(
      rowOverrides(collection({ ai_instructions: { mode: "own", text: "x" } }), LIBRARIES),
    ).toContain("AI instructions: own");
    const onDefault = rowOverrides(
      collection({ ai_instructions: { mode: "default", text: "" } }),
      LIBRARIES,
    );
    expect(onDefault).not.toContain("AI instructions: adds to the default");
    expect(onDefault).not.toContain("AI instructions: own");
  });

  it("badges length, year and rating limits only when set", () => {
    expect(
      rowOverrides(
        collection({ max_runtime: 120, min_year: 1990, max_year: 2010, min_rating: 7 }),
        LIBRARIES,
      ),
    ).toEqual(["Max length 120 min", "Released 1990–2010", "Rating 7+"]);
    expect(rowOverrides(collection({ min_year: 1990 }), LIBRARIES)).toEqual(["From 1990"]);
    expect(rowOverrides(collection({ max_year: 2010 }), LIBRARIES)).toEqual(["Up to 2010"]);
    expect(
      rowOverrides(
        collection({ max_runtime: null, min_year: null, max_year: null, min_rating: null }),
        LIBRARIES,
      ),
    ).toEqual([]);
  });

  it("badges AI instructions only when the row uses AI web search", () => {
    const own = { ai_instructions: { mode: "own", text: "x" } } as const;
    const withSearch = rowOverrides(
      collection({ ...own, candidate_sources: ["llm_web"] }),
      LIBRARIES,
      {},
    );
    expect(withSearch).toContain("AI instructions: own");
    const withoutSearch = rowOverrides(
      collection({ ...own, candidate_sources: ["trakt"] }),
      LIBRARIES,
      {},
    );
    expect(withoutSearch).not.toContain("AI instructions: own");
    // No sources of its own: it follows the global set, which decides.
    expect(
      rowOverrides(collection(own), LIBRARIES, { "candidates.sources": ["llm_web"] }),
    ).toContain("AI instructions: own");
    expect(
      rowOverrides(collection(own), LIBRARIES, { "candidates.sources": ["tmdb_similar"] }),
    ).not.toContain("AI instructions: own");
  });

  it("names the libraries a row is pinned to", () => {
    const parts = rowOverrides(collection({ library_keys: ["2"] }), LIBRARIES);
    expect(parts).toContain("Libraries: 4K Movies");
  });

  it("falls back to the key for a library the server no longer reports", () => {
    const parts = rowOverrides(collection({ library_keys: ["9"] }), LIBRARIES);
    expect(parts).toContain("Libraries: Library 9");
  });

  it("badges a row's own watched cap tersely, by percentage", () => {
    expect(rowOverrides(collection({ watched_pct: 0 }), LIBRARIES)).toContain(
      "Watched: all fresh",
    );
    expect(
      rowOverrides(collection({ watched_pct: 0.25 }), LIBRARIES),
    ).toContain("Watched: ≤25%");
    expect(rowOverrides(collection({ watched_pct: 1 }), LIBRARIES)).toContain(
      "Watched: no filter",
    );
  });

  it("shows no watched badge when the row inherits the global cap", () => {
    expect(rowOverrides(collection({ watched_pct: null }), LIBRARIES)).toEqual(
      [],
    );
  });

  it("badges a row's own cadence override, but not when it inherits the global one", () => {
    expect(rowOverrides(collection({ refresh_days: 0 }), LIBRARIES)).toContain(
      "Titles refresh: never",
    );
    expect(rowOverrides(collection({ refresh_days: 7 }), LIBRARIES)).toContain(
      "Titles refresh: every 7 days",
    );
    expect(rowOverrides(collection({ refresh_days: 1 }), LIBRARIES)).toContain(
      "Titles refresh: nightly",
    );
    expect(rowOverrides(collection({ refresh_days: null }), LIBRARIES)).toEqual(
      [],
    );
  });

  it("badges a narrowed placement and a pinned row, but not the default both/unpinned", () => {
    // Same placement for both -> simple badge
    expect(
      rowOverrides(
        collection({ placement: "home", placement_friends: "home" }),
        LIBRARIES,
      ),
    ).toContain("Shows on: Home");
    // Split placement -> shows both
    expect(
      rowOverrides(
        collection({ placement: "library", placement_friends: "both" }),
        LIBRARIES,
      ),
    ).toContain("Owner: Library · Friends: Home & Library");
    expect(rowOverrides(collection({ pin_top: true }), LIBRARIES)).toContain(
      "Pinned to top",
    );
    // Defaults (both/both / not pinned) add nothing.
    expect(
      rowOverrides(
        collection({
          placement: "both",
          placement_friends: "both",
          pin_top: false,
        }),
        LIBRARIES,
      ),
    ).toEqual([]);
  });

  it("badges the watched override even on the default row — the engine honours it there", () => {
    const parts = rowOverrides(
      collection({ slug: "picked", watched_pct: 0 }),
      LIBRARIES,
    );
    expect(parts).toContain("Watched: all fresh");
  });

  it("withholds the libraries part until the library list has loaded (no raw section keys)", () => {
    expect(rowOverrides(collection({ library_keys: ["2"] }), null)).toEqual([]);
  });

  it("lists every override at once", () => {
    const parts = rowOverrides(
      collection({
        candidate_sources: ["trakt"],
        library_keys: ["2"],
        watched_pct: 0,
        refresh_days: 1,
      }),
      LIBRARIES,
    );
    expect(parts).toEqual([
      "Sources: Trakt",
      "Libraries: 4K Movies",
      "Watched: all fresh",
      "Titles refresh: nightly",
    ]);
  });
});
