import { describe, expect, it } from "vitest";

import { latestSeq, mergeRunLog } from "@/lib/run-log";
import type { RunLogEntry } from "@/lib/types";

function entry(patch: Partial<RunLogEntry>): RunLogEntry {
  return {
    ts: "2026-07-15T04:18:00Z",
    run_id: 2,
    user: "sarah",
    stage: "history",
    counts: {},
    ...patch,
  };
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
    const live = entry({ ts: "2026-07-15T04:18:05Z", stage: "delivering" });
    const seededLater = entry({
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
    expect(out.map((e) => e.counts.done)).toEqual([1, 2]);
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

describe("latestSeq", () => {
  it("finds the highest seq, for asking the server only for what's new", () => {
    expect(
      latestSeq([entry({ seq: 3 }), entry({ seq: 11 }), entry({ seq: 7 })]),
    ).toBe(11);
  });

  it("is null when nothing carries a seq, meaning fetch the whole log", () => {
    expect(latestSeq([])).toBeNull();
    expect(latestSeq([entry({})])).toBeNull();
  });
});
