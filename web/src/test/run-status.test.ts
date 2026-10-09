import { describe, expect, it } from "vitest";

import { historyHint, runHealth } from "@/lib/run-status";
import type { Run } from "@/lib/types";

type Privacy = NonNullable<Run["privacy"]>;
type LoadedRun = Pick<Run, "status" | "privacy">;

function privacy(patch: Partial<Privacy> = {}): Privacy {
  return {
    can_see_others: [],
    unreadable_filters: [],
    filters_not_enforced: [],
    write_failed: [],
    unchecked: [],
    left_alone: [],
    ...patch,
  };
}

const flagged = privacy({ can_see_others: ["kid"] });
const clean = privacy();

describe("runHealth", () => {
  it("calls an ok run with a privacy finding 'OK · 1 warning', counting each flagged account", () => {
    expect(runHealth({ status: "ok", privacy: flagged })).toEqual({
      tone: "warn",
      label: "OK · 1 warning",
      warnings: 1,
    });
    const two = privacy({ ...flagged, unreadable_filters: ["jess"] });
    expect(runHealth({ status: "ok", privacy: two }).label).toBe("OK · 2 warnings");
  });
  it("warns when a run could not save someone's hide rules or could not look through their account", () => {
    const failed = privacy({ write_failed: ["jess"] });
    expect(runHealth({ status: "ok", privacy: failed })).toEqual({
      tone: "warn",
      label: "OK · 1 warning",
      warnings: 1,
    });
    const unchecked = privacy({ unchecked: ["kid", "Jess"] });
    expect(runHealth({ status: "ok", privacy: unchecked }).label).toBe("OK · 2 warnings");
  });
  it("counts a name once when several checks flag it, and does not warn for a left-alone account", () => {
    const both = privacy({ ...flagged, write_failed: ["Kid"] });
    expect(runHealth({ status: "ok", privacy: both }).warnings).toBe(1);
    const left = privacy({ left_alone: ["dad"] });
    expect(runHealth({ status: "ok", privacy: left }).label).toBe("OK");
  });
  it("keeps a clean ok run OK and a failed run Failed", () => {
    expect(runHealth({ status: "ok", privacy: clean }).label).toBe("OK");
    expect(runHealth({ status: "error", privacy: flagged })).toEqual({ tone: "error", label: "Failed", warnings: 0 });
  });
});

describe("historyHint", () => {
  const summary = { total: 3, ok: 3, error: 0, last_finished: null, last_status: null };
  it("reports warnings instead of 'all finished cleanly'", () => {
    const runs: LoadedRun[] = [{ status: "ok", privacy: flagged }, { status: "ok", privacy: clean }];
    expect(historyHint(summary, runs)).toBe("1 with warnings");
  });
  it("says all clean only when nothing warned", () => {
    expect(historyHint(summary, [{ status: "ok", privacy: clean }])).toBe("all finished cleanly");
  });
  it("lists failures first", () => {
    expect(historyHint({ ...summary, error: 1, ok: 2 }, [])).toBe("1 failed");
  });
});
