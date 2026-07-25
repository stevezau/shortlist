import { describe, expect, it } from "vitest";

import { rowOverrides } from "@/lib/collections";
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
    freshness: null,
    placement: "both",
    pin_top: false,
    hub_anchor: {},
    ...patch,
  } as Collection;
}

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

  it("badges a row's own freshness override, but not when it inherits the global one", () => {
    expect(rowOverrides(collection({ freshness: 0 }), LIBRARIES)).toContain(
      "Freshness: frozen",
    );
    expect(rowOverrides(collection({ freshness: 0.5 }), LIBRARIES)).toContain(
      "Freshness: 50%",
    );
    expect(rowOverrides(collection({ freshness: 1 }), LIBRARIES)).toContain(
      "Freshness: nightly",
    );
    expect(rowOverrides(collection({ freshness: null }), LIBRARIES)).toEqual(
      [],
    );
  });

  it("badges a narrowed placement and a pinned row, but not the default both/unpinned", () => {
    expect(
      rowOverrides(collection({ placement: "home" }), LIBRARIES),
    ).toContain("Shows on: Home");
    expect(
      rowOverrides(collection({ placement: "library" }), LIBRARIES),
    ).toContain("Shows on: Library");
    expect(rowOverrides(collection({ pin_top: true }), LIBRARIES)).toContain(
      "Pinned to top",
    );
    // Defaults (both / not pinned) add nothing.
    expect(
      rowOverrides(
        collection({ placement: "both", pin_top: false }),
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
        freshness: 1,
      }),
      LIBRARIES,
    );
    expect(parts).toEqual([
      "Sources: Trakt",
      "Libraries: 4K Movies",
      "Watched: all fresh",
      "Freshness: nightly",
    ]);
  });
});
