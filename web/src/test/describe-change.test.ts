/**
 * `describeChange` turns one audit event into the sentence the Activity page's "Changes on Plex" tab
 * shows. One example per Plex-write scope (`PLEX_WRITE_SCOPES`, services/audit.py), each built from
 * the `message` its emitter actually writes — the file:line is beside every example, so when an
 * emitter changes shape the example that has to change with it is easy to find.
 */
import { describe, expect, it } from "vitest";

import {
  changeFailed,
  changeMode,
  changeRunId,
  describeChange,
  isNoOpChange,
} from "@/lib/describe-change";
import type { AuditEvent } from "@/lib/types";

function event(scope: string, message: Record<string, unknown>, level = "info"): AuditEvent {
  return { id: 1, ts: "2026-10-03T02:31:30+00:00", level, scope, message };
}

const EXAMPLES: {
  scope: string;
  source: string;
  event: AuditEvent;
  who: string;
  what: string;
  change: string;
}[] = [
  {
    scope: "run.user",
    source: "run_persistence.py:1419",
    event: event("run.user", {
      run_id: 12,
      dry_run: false,
      user: "kid",
      status: "ok",
      diff: {
        added: ["Arrival", "Dune", "Heat", "Up", "Coco", "Big", "Jaws", "Alien", "Rocky", "Fargo"],
        removed: [],
        kept: [],
        deleted: [],
        duplicates_removed: [],
        collection_title: "Movies Picked for You",
        created: true,
        rating_key: 9123,
      },
      privacy_synced: true,
      llm_tokens: 0,
      exa_searches: 0,
      error: null,
    }),
    who: "kid",
    what: "Movies Picked for You",
    change: "+10 titles, collection created",
  },
  {
    scope: "run.shared",
    source: "run_persistence.py:1187",
    event: event("run.shared", {
      run_id: 12,
      dry_run: false,
      row: "shared_trending",
      status: "ok",
      picks: 20,
      error: null,
      reason: null,
      diff: {
        added: ["Severance", "Andor"],
        removed: ["Lost"],
        kept: [],
        deleted: [],
        duplicates_removed: [],
        collection_title: "Trending on Home Server",
        created: false,
        rating_key: 777,
      },
    }),
    who: "Trending on Home Server",
    what: "Shared row",
    change: "+2 titles, −1 title",
  },
  {
    scope: "run.sweep",
    source: "run_persistence.py:1454",
    event: event(
      "run.sweep",
      {
        run_id: 12,
        dry_run: false,
        reason: "row was broken beyond repair-in-place — no share filter could hide it",
        deleted: {
          sarah: ["TV Shows Picked for You"],
          "freed-name helper:Movies": ["Shortlist rename helper"],
        },
      },
      "warning",
    ),
    who: "sarah",
    what: "Swept before delivery",
    change: "Deleted 1 row no share filter could hide: TV Shows Picked for You, and 1 leftover helper collection",
  },
  {
    scope: "run.orphan_delete",
    source: "run_persistence.py:1512",
    event: event(
      "run.orphan_delete",
      {
        run_id: null,
        dry_run: false,
        job: "sync.check",
        reason: "its label names no one Shortlist knows",
        deleted: [
          {
            label: "shortlist_oldfriend",
            title: "Movies Picked for You",
            rating_key: 55,
            library_key: "1",
            library: "Movies",
          },
        ],
      },
      "warning",
    ),
    who: "Rows nobody owns",
    what: "Movies",
    change: "Deleted 1 collection whose person is no longer on the server: Movies Picked for You",
  },
  {
    scope: "run.privacy_sync",
    source: "run_persistence.py:1544",
    event: event("run.privacy_sync", {
      run_id: 12,
      dry_run: false,
      plex_account_id: 501,
      username: "mike",
      fields: {
        filterMovies: { before: "label!=shortlist_sarah", after: "label!=shortlist_sarah,shortlist_kid" },
        filterTelevision: { before: "", after: "label!=shortlist_kid" },
      },
    }),
    who: "mike",
    what: "Share filter",
    change: "Share filter merged: `label!=shortlist_kid` added",
  },
  {
    scope: "run.demote",
    source: "run_persistence.py:1487",
    event: event("run.demote", {
      run_id: 12,
      dry_run: false,
      demoted: [
        {
          label: "shortlist_jess",
          title: "Movies Picked for You",
          rating_key: 88,
          library_key: "1",
          library: "Movies",
          reason: "paused",
        },
      ],
    }),
    who: "jess",
    what: "Movies",
    change: "Taken off Home: Movies Picked for You (its person is paused)",
  },
  {
    scope: "run.hub_order",
    source: "run_persistence.py:1574",
    event: event("run.hub_order", {
      run_id: 12,
      dry_run: false,
      library: "Movies",
      anchor: "Recently Added Movies",
      moved: ["sarah", "mike", "jess", "kid", "Trending", "Movie night"],
      repositioned: 14,
      verified: true,
      reason: null,
      row: null,
    }),
    who: "Recommended shelf",
    what: "Movies",
    change: "Shelf order set: 6 rows placed, other tools' rows kept in their order",
  },
  {
    scope: "run.hub_unplaced",
    source: "run_persistence.py:1574",
    event: event(
      "run.hub_unplaced",
      {
        run_id: 12,
        dry_run: false,
        library: "TV Shows",
        anchor: "Continue Watching",
        moved: [],
        repositioned: null,
        verified: null,
        reason: "anchor not found",
        row: "Movie night",
      },
      "warning",
    ),
    who: "Movie night",
    what: "TV Shows",
    change: "Not placed on the shelf: anchor not found (Continue Watching)",
  },
  {
    // Queued titles that were already requested or already in the library wait nowhere, and an event
    // from before `waiting` existed cannot say either way: neither may claim anything is waiting.
    scope: "run.requests",
    source: "run_persistence.py (nothing waiting, and an older event with no `waiting`)",
    event: event("run.requests", { run_id: 12, dry_run: false, considered: 12, queued: 32, waiting: 0, sent: 0, outcomes: [] }),
    who: "Requests",
    what: "12 titles considered",
    change: "Nothing requested",
  },
  {
    scope: "run.requests",
    source: "run_persistence.py (an event recorded before `waiting` existed)",
    event: event("run.requests", { run_id: 12, dry_run: false, considered: 12, queued: 32, sent: 0, outcomes: [] }),
    who: "Requests",
    what: "12 titles considered",
    change: "Nothing requested",
  },
  {
    scope: "run.requests",
    source: "run_persistence.py:1644",
    event: event("run.requests", {
      run_id: 12,
      dry_run: false,
      considered: 12,
      queued: 3,
      waiting: 2,
      sent: 2,
      outcomes: [
        { tmdb_id: 1, title: "Dune: Part Two", media_type: "movie", status: "requested", detail: "Added to Radarr" },
        { tmdb_id: 2, title: "Shogun", media_type: "tv", status: "requested", detail: "Added to Sonarr" },
        { tmdb_id: 3, title: "Heat", media_type: "movie", status: "skipped_present", detail: "Already in Radarr" },
      ],
    }),
    who: "Requests",
    what: "12 titles considered",
    change: "Requested Dune: Part Two and Shogun; 2 titles waiting in Requests",
  },
  {
    scope: "collection.poster",
    source: "collection_reconcile.py:710 (a real reset)",
    event: event("collection.poster", {
      slug: "movie-night",
      poster_reset: ["Movies", "TV Shows"],
      dry_run: false,
      error: null,
    }),
    who: "movie-night",
    what: "Poster",
    change: "Poster reset to Plex's own artwork in Movies and TV Shows",
  },
  {
    scope: "collection.build",
    source: "collection_reconcile.py:737 via row_changes.py:131",
    event: event(
      "collection.build",
      { slug: "movie-night", removed: ["sarah's Movie night", "mike's Movie night"], dry_run: false, error: null },
      "warning",
    ),
    who: "movie-night",
    what: "Changed how it's built",
    change: "Removed from Plex: sarah's Movie night and mike's Movie night",
  },
  {
    scope: "collection.audience",
    source: "collection_reconcile.py:737 via row_changes.py:143",
    event: event(
      "collection.audience",
      { slug: "movie-night", removed: ["Movie night"], dry_run: true, error: null },
      "warning",
    ),
    who: "movie-night",
    what: "People taken out of its audience",
    change: "Would remove from Plex: Movie night",
  },
  {
    scope: "collection.disable",
    source: "collection_reconcile.py:737 via row_changes.py:149",
    event: event("collection.disable", { slug: "movie-night", removed: [], dry_run: false, error: null }, "warning"),
    who: "movie-night",
    what: "Switched off",
    change: "Nothing on Plex to remove",
  },
  {
    scope: "collection.libraries",
    source: "collection_reconcile.py:737 via row_changes.py:157",
    event: event(
      "collection.libraries",
      { slug: "movie-night", removed: ["TV Shows Movie night"], dry_run: false, error: null },
      "warning",
    ),
    who: "movie-night",
    what: "Left some libraries",
    change: "Removed from Plex: TV Shows Movie night",
  },
  {
    scope: "collection.delete",
    source: "collection_reconcile.py:737 via collections.py:1995",
    event: event(
      "collection.delete",
      { slug: "movie-night", removed: ["Movie night"], dry_run: false, error: "ReadTimeout: PMS took too long" },
      "warning",
    ),
    who: "movie-night",
    what: "Row deleted",
    change: "Couldn't finish: ReadTimeout: PMS took too long. Removed first: Movie night",
  },
  {
    scope: "collection.cleanup",
    source: "collection_reconcile.py:737 via collections.py:2178",
    event: event("collection.cleanup", { slug: "movie-night", removed: ["Movie night"], dry_run: false, error: null }, "warning"),
    who: "movie-night",
    what: "Removed from Plex now",
    change: "Removed from Plex: Movie night",
  },
  {
    scope: "collection.rename",
    source: "collection_reconcile.py:1167 via row_changes.py:173",
    event: event("collection.rename", {
      slug: "movie-night",
      new_template: "Friday films",
      error: null,
      renames: [
        { user: "sarah", display_name: "Sarah", old: "Movie night", new: "Friday films", libraries: ["Movies"] },
      ],
    }),
    who: "movie-night",
    what: "Renamed",
    change: "“Movie night” → “Friday films” in Movies",
  },
  {
    scope: "settings.rename",
    source: "collection_reconcile.py:1167 via settings.py:620",
    event: event("settings.rename", {
      slug: "picked",
      new_template: "{library_name} for You",
      error: null,
      renames: [
        { user: "sarah", display_name: "Sarah", old: "Movies Picked for You", new: "Movies for You", libraries: ["Movies"] },
        { user: "mike", display_name: "Mike", old: "Movies Picked for You", new: "Movies for You", libraries: ["Movies"] },
        { user: "kid", display_name: "Kid", old: "Movies Picked for You", new: "Movies for You", libraries: ["Movies"], next_run: true },
      ],
    }),
    who: "picked",
    what: "Name changed in Settings",
    change: "Renamed 2 collections, e.g. “Movies Picked for You” → “Movies for You”; 1 is retitled on its next run",
  },
  {
    scope: "user.nickname",
    source: "collection_reconcile.py:1167 via user_sync.py:122",
    event: event("user.nickname", {
      slug: "picked",
      new_template: "{library_name} Picked for {user}",
      error: null,
      renames: [
        {
          user: "sarah",
          display_name: "Sare",
          old: "Movies Picked for Sarah",
          new: "Movies Picked for Sare",
          libraries: ["Movies"],
        },
      ],
    }),
    who: "Sare",
    what: "Nickname changed",
    change: "“Movies Picked for Sarah” → “Movies Picked for Sare” in Movies",
  },
  {
    scope: "user.disable.cleanup",
    source: "jobs.py:1428",
    event: event(
      "user.disable.cleanup",
      { user: "sarah", removed: ["Movies Picked for You", "TV Shows Picked for You"], dry_run: false },
      "warning",
    ),
    who: "sarah",
    what: "Turned off",
    change: "Rows removed from Plex: Movies Picked for You and TV Shows Picked for You",
  },
  {
    scope: "user.pause.hide",
    source: "jobs.py:1461",
    event: event("user.pause.hide", { user: "jess", hidden: 2, dry_run: false }),
    who: "jess",
    what: "Paused",
    change: "2 rows taken off Home and Recommended; the collections stay on Plex",
  },
  {
    scope: "uninstall.user",
    source: "api/system.py:958 (restored)",
    event: event(
      "uninstall.user",
      {
        user: "mike",
        restored_to: { filterMovies: "contentRating!=R", filterTelevision: "" },
        dry_run: false,
      },
      "warning",
    ),
    who: "mike",
    what: "Uninstall",
    change: "Share filter put back the way it was before Shortlist",
  },
  {
    scope: "system.uninstall",
    source: "api/system.py:963",
    event: event(
      "system.uninstall",
      {
        filters_restored: 12,
        filters_skipped: [],
        filters_unreachable: [],
        filters_failed: [],
        collections_deleted: ["Movies Picked for You", "TV Shows Picked for You", "Trending"],
        rows_disabled: 4,
        dry_run: true,
        accounts_listed: 14,
        at: "2026-10-03T05:00:00+00:00",
      },
      "warning",
    ),
    who: "Whole server",
    what: "Uninstall",
    change: "Would restore 12 share filters, delete 3 collections and switch off 4 rows",
  },
  {
    scope: "privacy.restriction_restored",
    source: "audit.py:118",
    event: event("privacy.restriction_restored", { account_id: 501, username: "kid" }),
    who: "kid",
    what: "Share filter",
    change: "Repaired: the restriction you set in Plex applies again",
  },
];

describe("describeChange — one recorded example per Plex-write scope", () => {
  it.each(EXAMPLES)("$scope ($source)", ({ event: e, who, what, change }) => {
    expect(describeChange(e)).toEqual({ who, what, change });
  });

  it("covers every scope the server counts as a Plex write", () => {
    // The list in services/audit.py `PLEX_WRITE_SCOPES`, copied here so a scope added there without
    // a sentence here fails this test rather than falling through to the generic summary unnoticed.
    const serverScopes = [
      "run.user",
      "run.shared",
      "run.sweep",
      "run.orphan_delete",
      "run.privacy_sync",
      "run.demote",
      "run.hub_order",
      "run.hub_unplaced",
      "run.requests",
      "collection.poster",
      "collection.build",
      "collection.audience",
      "collection.disable",
      "collection.libraries",
      "collection.delete",
      "collection.cleanup",
      "collection.rename",
      "settings.rename",
      "user.nickname",
      "user.disable.cleanup",
      "user.pause.hide",
      "uninstall.user",
      "system.uninstall",
      "privacy.restriction_restored",
    ];
    expect(new Set(EXAMPLES.map((example) => example.scope))).toEqual(new Set(serverScopes));
  });
});

describe("describeChange — the variants inside a scope", () => {
  it("says a settings-only poster change wrote nothing to Plex", () => {
    // collections.py:1411 writes `collection.poster` when only the stored setting changed: it carries
    // `mode`, not `poster_reset`, and nothing reached Plex.
    const e = event("collection.poster", { slug: "movie-night", mode: "upload", at: "2026-10-03T01:00:00+00:00" });

    expect(describeChange(e)).toEqual({
      who: "movie-night",
      what: "Poster setting",
      change: "Poster setting saved as an uploaded image. This change wrote nothing to Plex",
    });
    expect(changeMode(e)).toBe("setting");
  });

  it("says what a dry run of a person's row would have done, in the conditional", () => {
    const e = event("run.user", {
      run_id: 13,
      dry_run: true,
      user: "sarah",
      status: "ok",
      diff: { added: Array.from({ length: 10 }, (_, i) => `T${i}`), removed: [], deleted: [], duplicates_removed: [], collection_title: "Movies Picked for You", created: true },
      error: null,
    });

    expect(describeChange(e).change).toBe("Would add 10 titles and create the collection");
  });

  it("names the failure instead of a diff when a person's row errored", () => {
    const e = event(
      "run.user",
      { run_id: 13, dry_run: false, user: "sarah", status: "error", diff: {}, error: "TMDB timed out" },
      "error",
    );

    expect(describeChange(e).change).toBe("Failed: TMDB timed out");
    expect(changeFailed(e)).toBe(true);
  });

  it("phrases a dry-run share-filter merge as what it would do", () => {
    const e = event("run.privacy_sync", {
      run_id: 14,
      dry_run: true,
      plex_account_id: 7,
      username: "jess",
      fields: { filterMovies: { before: "", after: "label!=shortlist_sarah" } },
    });

    expect(describeChange(e).change).toBe("Would merge `label!=shortlist_sarah` into the share filter");
  });

  it("reads plexapi's %2C-joined values as separate rules, so only the new one is named", () => {
    const e = event("run.privacy_sync", {
      run_id: 14,
      dry_run: false,
      plex_account_id: 7,
      username: "jess",
      fields: {
        filterMovies: {
          before: "contentRating!=R|label!=shortlist_sarah",
          after: "contentRating!=R&label!=shortlist_sarah%2Cshortlist_kid",
        },
      },
    });

    expect(describeChange(e).change).toBe("Share filter merged: `label!=shortlist_kid` added");
  });

  it("names a removed exclude too (leaving an account's sharing alone takes ours out)", () => {
    const e = event("run.privacy_sync", {
      run_id: 15,
      dry_run: false,
      plex_account_id: 7,
      username: "jess",
      fields: { filterMovies: { before: "label!=shortlist_sarah,Kids", after: "label!=Kids" } },
    });

    expect(describeChange(e).change).toBe("Share filter merged: `label!=shortlist_sarah` removed");
  });

  it("says when the shelf did not read back in the order Shortlist asked for", () => {
    const e = event(
      "run.hub_order",
      { run_id: 12, dry_run: false, library: "Movies", anchor: "", moved: ["a", "b"], repositioned: 2, verified: false, reason: null, row: null },
      "warning",
    );

    expect(describeChange(e).change).toBe("Asked Plex to place 2 rows, but the shelf didn't read back in that order");
    expect(changeFailed(e)).toBe(true);
  });

  it("says an uninstall restore that could not be confirmed", () => {
    const e = event(
      "uninstall.user",
      { user: "mike", attempted: { filterMovies: "" }, verified: false, error: "plex.tv answered 500", dry_run: false },
      "warning",
    );

    expect(describeChange(e).change).toBe("Restore not confirmed: plex.tv answered 500");
    expect(changeFailed(e)).toBe(true);
  });

  it("uses row and person names when the page can supply them", () => {
    const names = {
      row: (slug: string) => (slug === "movie-night" ? "Movie night" : undefined),
      person: (slug: string) => (slug === "sarah" ? "Sarah" : undefined),
    };

    expect(describeChange(EXAMPLES.find((x) => x.scope === "collection.cleanup")!.event, names).who).toBe("Movie night");
    expect(describeChange(EXAMPLES.find((x) => x.scope === "run.sweep")!.event, names).who).toBe("Sarah");
  });

  it("treats the run-less shelf scopes like their run twins", () => {
    // jobs.py:1068 — the same keys as `run.hub_order`, written by a job, with no run_id.
    const e = event("shelf.order", { dry_run: false, library: "Movies", anchor: "", moved: ["a"], repositioned: 1, verified: true, reason: null, row: null });

    expect(describeChange(e)).toEqual({ who: "Recommended shelf", what: "Movies", change: "Shelf order set: 1 row placed" });
  });
});

describe("describeChange — a scope it does not know", () => {
  it("falls back to the scope name and a short summary of the message", () => {
    const e = event("row.new_thing", { run_id: 3, slug: "picked", count: 4, titles: ["a", "b"], nested: { x: 1 } });

    expect(describeChange(e)).toEqual({
      who: "row.new_thing",
      what: "",
      change: "slug: picked, count: 4, titles: 2 items, nested: {…}",
    });
  });

  it("keeps a long summary to one line", () => {
    const e = event("row.new_thing", { note: "x".repeat(400) });

    expect(describeChange(e).change.length).toBeLessThanOrEqual(160);
    expect(describeChange(e).change.endsWith("…")).toBe(true);
  });
});

describe("the facts the table needs beside the sentence", () => {
  it("reads real, dry run, or not recorded from the message", () => {
    expect(changeMode(event("run.user", { dry_run: false }))).toBe("real");
    expect(changeMode(event("run.user", { dry_run: true }))).toBe("dry");
    // Renames and the restriction repair record no dry_run at all; claiming "Real" would be a guess.
    expect(changeMode(event("collection.rename", { slug: "x", renames: [] }))).toBe("unrecorded");
  });

  it("finds the run an event belongs to, and none for a job or an edit", () => {
    expect(changeRunId(event("run.user", { run_id: 12 }))).toBe(12);
    expect(changeRunId(event("run.privacy_sync", { run_id: null, job: "privacy.sync" }))).toBeNull();
    expect(changeRunId(event("collection.cleanup", { slug: "x" }))).toBeNull();
  });

  it("knows a person's row that changed nothing on Plex", () => {
    const unchanged = event("run.user", {
      run_id: 12,
      dry_run: false,
      user: "mike",
      status: "ok",
      diff: { added: [], removed: [], kept: ["a", "b"], deleted: [], duplicates_removed: [], collection_title: "Movies Picked for You", created: false },
      error: null,
    });

    expect(isNoOpChange(unchanged)).toBe(true);
    expect(isNoOpChange(EXAMPLES[0]!.event)).toBe(false);
    // A failure is never folded away, even when it changed nothing.
    expect(isNoOpChange(event("run.user", { run_id: 12, status: "error", diff: {}, error: "boom" }, "error"))).toBe(false);
  });
});
