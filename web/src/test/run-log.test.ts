import { describe, expect, it } from "vitest";

import { latestSeq, mergeRunLog, stageBelongsToRun } from "@/lib/run-log";
import type { RunLogEntry } from "@/lib/types";

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

/**
 * An entry from a server old enough not to stamp `seq`. Both sources the log merges — the durable
 * `RunLogLineOut` read and the SSE stage stream — always carry one now, so the legacy shape has to
 * be built deliberately; without it `logKey`'s ts|user|stage fallback would go unasserted.
 */
function legacyEntry(patch: Partial<RunLogEntry>): RunLogEntry {
  return { ...entry(patch), seq: undefined } as unknown as RunLogEntry;
}

describe("mergeRunLog", () => {
  it("appends events for this run and drops events for a different run", () => {
    const out = mergeRunLog(
      [],
      [
        entry({ ts: "2026-07-15T04:18:01Z", run_id: 2, stage: "history" }),
        entry({ ts: "2026-07-15T04:18:02Z", run_id: 9, stage: "curating" }), // another run
      ],
      2,
    );
    expect(out.map((e) => e.stage)).toEqual(["history"]);
  });

  it("keeps an event with no run_id (belongs to the single in-flight run)", () => {
    const out = mergeRunLog([], [entry({ run_id: null })], 2);
    expect(out).toHaveLength(1);
  });

  it("dedups the same event arriving from both the seed snapshot and the live stream", () => {
    const seed = entry({ ts: "2026-07-15T04:18:03Z", stage: "candidates" });
    const afterSeed = mergeRunLog([], [seed], 2);
    // The identical event later arrives over SSE — it must not double.
    const afterLive = mergeRunLog(afterSeed, [{ ...seed }], 2);
    expect(afterLive).toHaveLength(1);
  });

  it("orders merged events by timestamp regardless of arrival order", () => {
    const live = legacyEntry({ ts: "2026-07-15T04:18:05Z", stage: "delivering" });
    const seededLater = legacyEntry({
      ts: "2026-07-15T04:18:04Z",
      stage: "curating",
    });
    // The live event arrived first, then the earlier seed snapshot — the feed still reads in order.
    const out = mergeRunLog(mergeRunLog([], [live], 2), [seededLater], 2);
    expect(out.map((e) => e.stage)).toEqual(["curating", "delivering"]);
  });

  it("returns the same array reference when nothing new is added", () => {
    const prev = mergeRunLog([], [entry({})], 2);
    expect(mergeRunLog(prev, [entry({ run_id: 9 })], 2)).toBe(prev);
  });

  it("keeps progress lines that share a timestamp and a stage", () => {
    // "merging share filters 1/5" and "2/5" are the same user, stage and millisecond. Keyed on
    // ts|user|stage they collapsed into one line and the phase looked frozen at 1/5.
    const out = mergeRunLog(
      [],
      [
        entry({
          seq: 0,
          user: "Shortlist",
          stage: "filters",
          counts: { done: 1, total: 5 },
        }),
        entry({
          seq: 1,
          user: "Shortlist",
          stage: "filters",
          counts: { done: 2, total: 5 },
        }),
      ],
      2,
    );
    expect(out).toHaveLength(2);
    expect(out.map((e) => e.counts?.done)).toEqual([1, 2]);
  });

  it("still dedupes the same seq arriving from both the seed fetch and SSE", () => {
    const line = entry({ seq: 7, stage: "promoting" });
    const seeded = mergeRunLog([], [line], 2);
    expect(mergeRunLog(seeded, [{ ...line }], 2)).toHaveLength(1);
  });

  it("orders by seq, which is the order the engine emitted them", () => {
    // Two lines a millisecond apart in the wrong direction: seq is the truth, not the clock.
    const later = entry({
      seq: 2,
      ts: "2026-07-15T04:18:00Z",
      stage: "ordering",
    });
    const earlier = entry({
      seq: 1,
      ts: "2026-07-15T04:18:01Z",
      stage: "promoting",
    });
    const out = mergeRunLog([], [later, earlier], 2);
    expect(out.map((e) => e.stage)).toEqual(["promoting", "ordering"]);
  });
});

describe("stageBelongsToRun", () => {
  // The same predicate mergeRunLog uses to filter — asserted directly so a caller deciding whether
  // to REFETCH (not just whether to log) can rely on identical semantics (issue: run-detail's SSE
  // handler used to refetch on every stage event regardless of which run it belonged to).
  it("matches this run's id, and tolerates a missing run_id", () => {
    expect(stageBelongsToRun({ run_id: 2 }, 2)).toBe(true);
    expect(stageBelongsToRun({ run_id: null }, 2)).toBe(true);
    expect(stageBelongsToRun({}, 2)).toBe(true);
  });

  it("rejects a different run's id", () => {
    expect(stageBelongsToRun({ run_id: 40 }, 2)).toBe(false);
  });
});

describe("latestSeq", () => {
  it("finds the highest seq, for asking the server only for what's new", () => {
    expect(
      latestSeq([entry({ seq: 3 }), entry({ seq: 11 }), entry({ seq: 7 })]),
    ).toBe(11);
  });

  it("is null when nothing carries a seq, meaning fetch the whole log", () => {
    expect(latestSeq([])).toBeNull();
    expect(latestSeq([legacyEntry({})])).toBeNull();
  });
});
