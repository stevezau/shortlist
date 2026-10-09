import { describe, expect, it } from "vitest";

import { historyHint, runHealth } from "@/lib/run-status";

const flagged = { can_see_others: ["kid"], unreadable_filters: [], filters_not_enforced: [] };
const clean = { can_see_others: [], unreadable_filters: [], filters_not_enforced: [] };

describe("runHealth", () => {
  it("calls an ok run with a privacy finding 'OK · 1 warning', counting each flagged account", () => {
    expect(runHealth({ status: "ok", privacy: flagged as never })).toEqual({
      tone: "warn",
      label: "OK · 1 warning",
      warnings: 1,
    });
    const two = { ...flagged, unreadable_filters: ["jess"] };
    expect(runHealth({ status: "ok", privacy: two as never }).label).toBe("OK · 2 warnings");
  });
  it("keeps a clean ok run OK and a failed run Failed", () => {
    expect(runHealth({ status: "ok", privacy: clean as never }).label).toBe("OK");
    expect(runHealth({ status: "error", privacy: flagged as never })).toEqual({ tone: "error", label: "Failed", warnings: 0 });
  });
});

describe("historyHint", () => {
  const summary = { total: 3, ok: 3, error: 0, last_finished: null, last_status: null };
  it("reports warnings instead of 'all finished cleanly'", () => {
    const runs = [{ status: "ok", privacy: flagged }, { status: "ok", privacy: clean }];
    expect(historyHint(summary, runs as never)).toBe("1 with warnings");
  });
  it("says all clean only when nothing warned", () => {
    expect(historyHint(summary, [{ status: "ok", privacy: clean }] as never)).toBe("all finished cleanly");
  });
  it("lists failures first", () => {
    expect(historyHint({ ...summary, error: 1, ok: 2 }, [] as never)).toBe("1 failed");
  });
});
