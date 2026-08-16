import { describe, expect, it } from "vitest";

import {
  currentPhase,
  errorBucket,
  webSearchSummary,
  friendlyError,
  rankClass,
  tokenStepBreakdown,
} from "@/lib/run-format";
import type { RunDetail, RunLogEntry } from "@/lib/types";

describe("friendlyError", () => {
  it("recognises a Plex 500, a timeout and a 429 with their own sentence", () => {
    expect(friendlyError("HTTP 500 internal_server_error")).toMatch(
      /server error \(500\)/,
    );
    expect(friendlyError("request timed out after 30s")).toMatch(/timed out/);
    expect(friendlyError("429 too many requests")).toMatch(/rate-limiting/);
  });

  it("falls back to one generic sentence for anything unrecognised", () => {
    expect(friendlyError("KeyError: 'ratingKey'")).toBe(
      "Something went wrong building this person’s row.",
    );
    expect(friendlyError("connection refused to 10.0.0.5:32400")).toBe(
      "Something went wrong building this person’s row.",
    );
  });
});

describe("errorBucket — only the three recognised classes count as 'the same problem'", () => {
  it("groups two different raw 500s into the same bucket", () => {
    expect(errorBucket("HTTP 500 while updating collection 123")).toBe(
      errorBucket("HTTP 500 while updating collection 456"),
    );
  });

  it("does not group unrelated unrecognised errors — the bug this fixes", () => {
    // Both fell through to friendlyError's one generic sentence before this fix, so bucketing on
    // that return value (the old errorBucket) merged them into a false "same problem" claim.
    const a = errorBucket("KeyError: 'ratingKey'");
    const b = errorBucket("connection refused to 10.0.0.5:32400");
    const c = errorBucket("AttributeError: NoneType has no attribute 'guid'");
    expect(a).toBeNull();
    expect(b).toBeNull();
    expect(c).toBeNull();
    // null !== null is false in a Map key sense (Map treats null as one key), so the CALLER must
    // treat a null bucket as "never counted" rather than grouping every unrecognised error together.
    // errorBucket itself only promises: recognised errors bucket by class, everything else is null.
  });

  it("keeps the three recognised classes distinct from one another", () => {
    const classes = new Set([
      errorBucket("HTTP 500"),
      errorBucket("timed out"),
      errorBucket("429 too many requests"),
    ]);
    expect(classes.size).toBe(3);
  });
});

describe("rankClass", () => {
  it("tiers 1-3, 4-10, and the rest", () => {
    expect(rankClass(1)).toContain("amber");
    expect(rankClass(3)).toContain("amber");
    expect(rankClass(4)).not.toContain("amber");
    expect(rankClass(10)).not.toContain("muted");
    expect(rankClass(11)).toContain("muted");
  });
});

describe("tokenStepBreakdown", () => {
  it("formats the by-step map, biggest first, dropping zeros", () => {
    expect(
      tokenStepBreakdown({ curate: 251295, llm_web: 126133, llm_library: 0 }),
    ).toBe("final picks 251,295 · web search 126,133");
  });

  it("returns '' for undefined — the single shape both callers (parens or not) build on", () => {
    expect(tokenStepBreakdown(undefined)).toBe("");
  });
});

describe("webSearchSummary", () => {
  it("says nothing when zero or undefined", () => {
    expect(webSearchSummary(undefined)).toBe("");
    expect(webSearchSummary(0)).toBe("");
  });

  it("pluralises correctly", () => {
    expect(webSearchSummary(1)).toBe(" · 1 web search");
    expect(webSearchSummary(2)).toBe(" · 2 web searches");
  });

  it("does not name a vendor — the same counter serves Exa and SearXNG", () => {
    // The stat counts external searches whichever backend ran them; saying "Exa" here would be
    // simply false on a self-hosted server.
    expect(webSearchSummary(3)).not.toMatch(/Exa/i);
  });
});

function entry(patch: Partial<RunLogEntry>): RunLogEntry {
  return {
    seq: 0,
    ts: "2026-07-15T04:18:00Z",
    run_id: 2,
    user: "sarah",
    stage: "history",
    counts: {},
    ...patch,
  };
}

/** A run whose roster is `slugs`, with `finishedSlugs` already reported on. The header counts off
 *  these two fields, exactly as the Rows tab does. */
function runWith(slugs: string[], finishedSlugs: string[] = []): RunDetail {
  return {
    stats: { expected_users: slugs.map((slug) => ({ slug })) },
    users: finishedSlugs.map((slug) => ({ slug, status: "ok" })),
    shared_rows: [],
  } as unknown as RunDetail;
}

describe("currentPhase", () => {
  it("names the latest tail stage, not a per-person one", () => {
    const phase = currentPhase(runWith(["sarah"], ["sarah"]), [
      entry({ user: "sarah", stage: "curating" }),
      entry({ user: "Shortlist", stage: "converging" }),
    ]);
    expect(phase?.label).toMatch(/stranded/i);
    expect(phase?.tail).toBe(true);
  });

  it("returns null once the server-wide finished marker is the latest", () => {
    const phase = currentPhase(runWith(["sarah"], ["sarah"]), [
      entry({ user: "Shortlist", stage: "converging" }),
      entry({ user: "Shortlist", stage: "finished" }),
    ]);
    expect(phase).toBeNull();
  });

  it("returns null with nothing but queued people recorded yet", () => {
    expect(
      currentPhase(runWith(["sarah", "mike"]), [
        entry({ user: "sarah", stage: "queued" }),
        entry({ user: "mike", stage: "queued" }),
      ]),
    ).toBeNull();
  });

  it("keeps naming setup while the run is between preparing and its first person", () => {
    const phase = currentPhase(runWith(["sarah"]), [
      entry({ user: "sarah", stage: "queued" }),
      entry({ user: "Shortlist", stage: "preparing" }),
    ]);
    expect(phase?.label).toMatch(/reading your libraries/i);
    expect(phase?.tail).toBe(false);
  });

  it("does not let the LIBRARY INDEX start the per-user stretch or pad its total", () => {
    // `_build_indexes` narrates under the SECTION TITLE, not a slug (pipeline.py:214-220), and it
    // runs inside `preparing`. Counting log subjects made "Movies"/"TV Shows" two extra people AND
    // declared the run was building rows while it was still reading libraries.
    const phase = currentPhase(runWith(["sarah", "mike", "ana"]), [
      entry({ user: "sarah", stage: "queued" }),
      entry({ user: "mike", stage: "queued" }),
      entry({ user: "ana", stage: "queued" }),
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "Movies", stage: "indexing" }),
      entry({ user: "TV Shows", stage: "indexed", counts: { items: 900 } }),
    ]);
    expect(phase).toEqual({
      label: "getting ready — reading your libraries",
      tail: false,
    });
  });

  it("does not let a SHARED row start the per-user stretch either", () => {
    // A shared row narrates under `shared_<row>` (rows.py:2480,2667). It is the ONLY non-server
    // subject here on purpose: with a real person's line in the fixture too, the start gate would
    // be satisfied regardless and this would pin nothing.
    const phase = currentPhase(runWith(["sarah", "mike", "ana"]), [
      entry({ user: "sarah", stage: "queued" }),
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "shared_popular", stage: "delivering" }),
    ]);
    expect(phase).toEqual({
      label: "getting ready — reading your libraries",
      tail: false,
    });
  });

  it("names the shared-row build instead of freezing on 'N of N people done'", () => {
    // Between the last person's terminal emit and `users_done`, `_deliver_phase` is still building
    // shared rows — which belong to no person, so the people count cannot move. The header used to
    // sit on "3 of 3 people done" for the whole window, which is the wedged look it exists to fix.
    const run = {
      stats: {
        expected_users: ["sarah", "mike", "ana"].map((slug) => ({ slug })),
        expected_rows: [
          { slug: "picked" },
          { slug: "popular", build: "shared" },
        ],
      },
      users: ["sarah", "mike", "ana"].map((slug) => ({ slug, status: "ok" })),
      shared_rows: [],
    } as unknown as RunDetail;
    const phase = currentPhase(run, [
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "ana", stage: "done" }),
      entry({ user: "shared_popular", stage: "delivering" }),
    ]);
    expect(phase).toEqual({ label: "building the shared row", tail: false });
  });

  it("keeps the people line at N of N when the run has no shared row to build", () => {
    const phase = currentPhase(runWith(["sarah", "mike"], ["sarah", "mike"]), [
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "mike", stage: "done" }),
    ]);
    expect(phase?.label).toBe("building rows — 2 of 2 people done");
  });

  it("counts people once anyone has started, rather than repeating a stale preparing", () => {
    // Run #10: `preparing` is the newest SERVER line for the entire per-user stretch, so reading
    // back to it reported "getting ready — reading your libraries" 9 people into 46, under a
    // "Finishing up" lead-in. Neither half was true.
    const phase = currentPhase(runWith(["sarah", "mike", "ana"], ["sarah"]), [
      entry({ user: "sarah", stage: "queued" }),
      entry({ user: "mike", stage: "queued" }),
      entry({ user: "ana", stage: "queued" }),
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "sarah", stage: "history" }),
      entry({ user: "sarah", stage: "done" }),
      entry({ user: "mike", stage: "curating" }),
    ]);
    expect(phase).toEqual({
      label: "building rows — 1 of 3 people done",
      tail: false,
    });
  });

  it("counts a person the run has reported on however it ended", () => {
    // skipped and error are terminal too — a run of 3 where 2 failed is not "0 of 3 done".
    const run = {
      stats: {
        expected_users: ["sarah", "mike", "ana"].map((slug) => ({ slug })),
      },
      users: [
        { slug: "sarah", status: "skipped" },
        { slug: "mike", status: "error" },
        { slug: "ana", status: "pending" },
      ],
      shared_rows: [],
    } as unknown as RunDetail;
    const phase = currentPhase(run, [
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "ana", stage: "delivering" }),
    ]);
    expect(phase?.label).toBe("building rows — 2 of 3 people done");
  });

  it("hands back to the tail the moment users_done lands", () => {
    const phase = currentPhase(runWith(["sarah"], ["sarah"]), [
      entry({ user: "sarah", stage: "queued" }),
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "sarah", stage: "done" }),
      entry({
        user: "Shortlist",
        stage: "filters",
        counts: { done: 12, total: 46 },
      }),
    ]);
    expect(phase).toEqual({
      label: "merging share filters 12/46",
      tail: true,
    });
  });

  it("counts nothing rather than 'N of N' when the run declared no roster", () => {
    // A run recorded before `expected_users` existed. Counting `run.users` instead looks like a
    // safe fallback and is not: the pending entries there are synthesised FROM that same roster, so
    // without it everyone present reads as finished and a run mid-flight claims it is done. Falling
    // through to the server's own phase understates what is happening; it does not misstate it.
    const run = {
      stats: {},
      users: [
        { slug: "sarah", status: "ok" },
        { slug: "mike", status: "ok" },
      ],
      shared_rows: [],
    } as unknown as RunDetail;
    const phase = currentPhase(run, [
      entry({ user: "Shortlist", stage: "preparing" }),
      entry({ user: "mike", stage: "curating" }),
    ]);
    expect(phase?.label).toBe("getting ready — reading your libraries");
  });
});
/** Run #10 on the owner's server, 2026-08-17 — 46 people, 94m26s, replayed from the shape its real
 *  522-line log had. The run this whole fix came from: it spent 5037s (84 min) with `preparing` as
 *  the newest server line and 572s more with a single `ordering` line, so the header was wrong or
 *  frozen for all but a few seconds of it.
 *
 *  The roster comes from `stats.expected_users`, written at queue time AND at start
 *  (run_service.py:180,266) and dropped only when `run_persistence.py:782` reassigns `stats` whole
 *  at finalize — the same moment `finished_at` is set and this header stops rendering. `run.users`
 *  grows as people finish because `ctx.on_user_done` persists each person BEFORE their terminal
 *  emit (pipeline.py:373 then :380), so a person present in the log is present in the payload.
 */
describe("currentPhase — replaying run #10", () => {
  const ROSTER = Array.from({ length: 46 }, (_, i) => `person${i + 1}`);
  const line = (user: string, stage: string, counts = {}) =>
    entry({ user, stage, counts });

  /** The run as the page saw it when `done` people had finished. */
  const runAt = (done: number): RunDetail =>
    ({
      stats: {
        expected_users: ROSTER.map((slug) => ({ slug })),
        expected_rows: [
          { slug: "picked", build: "per_person" },
          { slug: "because", build: "per_person" },
          { slug: "popular", build: "shared" },
        ],
      },
      users: ROSTER.slice(0, done).map((slug) => ({ slug, status: "ok" })),
      shared_rows: [],
    }) as unknown as RunDetail;

  const history = ROSTER.map((_, i) =>
    line("Shortlist", "reading_history", { done: i + 1, total: 46 }),
  );
  const queued = ROSTER.map((slug) => line(slug, "queued"));
  const preparing = [line("Shortlist", "preparing")];
  const through = (n: number) =>
    ROSTER.slice(0, n).flatMap((slug) => [
      line(slug, "history"),
      line(slug, "curating"),
      line(slug, "done"),
    ]);

  it("names the watch-history pre-read before anyone is queued", () => {
    expect(currentPhase(runAt(0), history)).toEqual({
      label: "reading watch history 46/46",
      tail: false,
    });
  });

  it("names the index build between queueing and the first person", () => {
    expect(
      currentPhase(runAt(0), [...history, ...queued, ...preparing]),
    ).toEqual({
      label: "getting ready — reading your libraries",
      tail: false,
    });
  });

  it("reproduces the reported screenshot — 9 people in, NOT 'finishing up'", () => {
    // What the owner saw: "Finishing up · getting ready — reading your libraries", beside a Rows
    // tab correctly reading "9 of 46 people done". This is that exact moment.
    const phase = currentPhase(runAt(9), [
      ...history,
      ...queued,
      ...preparing,
      ...through(9),
    ]);
    expect(phase).toEqual({
      label: "building rows — 9 of 46 people done",
      tail: false,
    });
  });

  it("names the shared-row build in the gap before users_done", () => {
    expect(
      currentPhase(runAt(46), [
        ...history,
        ...queued,
        ...preparing,
        ...through(46),
        line("shared_popular", "delivering"),
      ]),
    ).toEqual({ label: "building the shared row", tail: false });
  });

  it("only says 'Finishing up' once the tail actually starts", () => {
    const tail = [
      ...history,
      ...queued,
      ...preparing,
      ...through(46),
      line("Shortlist", "users_done", { done: 46, total: 46 }),
      line("Shortlist", "filters", { done: 12, total: 46 }),
    ];
    expect(currentPhase(runAt(46), tail)).toEqual({
      label: "merging share filters 12/46",
      tail: true,
    });
    // The 572-second phase the owner watched sit still. 186 collections = 46 people x 2 rows x 2
    // libraries, plus the shared row in each.
    expect(
      currentPhase(runAt(46), [
        ...tail,
        line("Shortlist", "ordering", { done: 40, total: 186 }),
      ]),
    ).toEqual({ label: "ordering rows 40/186", tail: true });
  });

  it("goes quiet on the finished marker", () => {
    expect(
      currentPhase(runAt(46), [
        ...history,
        ...preparing,
        ...through(46),
        line("Shortlist", "finished", { ok: 46, failed: 0, seconds: 5666 }),
      ]),
    ).toBeNull();
  });
});
