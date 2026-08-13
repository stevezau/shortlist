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

  it("treats a row being created as having nothing to differ from", () => {
    expect(hasUnsavedChanges(toInput(collection()), null)).toBe(false);
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
      "Rebuilds: never",
    );
    expect(rowOverrides(collection({ refresh_days: 7 }), LIBRARIES)).toContain(
      "Rebuilds: every 7 days",
    );
    expect(rowOverrides(collection({ refresh_days: 1 }), LIBRARIES)).toContain(
      "Rebuilds: nightly",
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
      "Rebuilds: nightly",
    ]);
  });
});
