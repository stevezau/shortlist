import { describe, expect, it } from "vitest";

import { mergeRunLog } from "@/lib/run-log";
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
});
