import { describe, expect, it } from "vitest";
import { latestFinishedRun, nextRowRun } from "@/lib/dashboard-status";
import type { Run, ScheduleResponse } from "@/lib/types";

const run = (id: number, status: string) => ({ id, status }) as unknown as Run;
const schedule = (rows: { next_run: string | null; cron: string }[]) =>
  ({ rows }) as unknown as ScheduleResponse;

describe("latestFinishedRun", () => {
  it("returns undefined without runs", () => {
    expect(latestFinishedRun(undefined)).toBeUndefined();
    expect(latestFinishedRun([])).toBeUndefined();
  });

  it("skips runs still in flight and returns the first finished one", () => {
    const runs = [run(3, "running"), run(2, "queued"), run(1, "ok")];
    expect(latestFinishedRun(runs)?.id).toBe(1);
  });

  it("counts a failed run as finished", () => {
    expect(latestFinishedRun([run(2, "error"), run(1, "ok")])?.id).toBe(2);
  });

  it("returns undefined when nothing has finished", () => {
    expect(latestFinishedRun([run(1, "running")])).toBeUndefined();
  });
});

describe("nextRowRun", () => {
  it("returns undefined with no schedule or no upcoming run", () => {
    expect(nextRowRun(undefined)).toBeUndefined();
    expect(nextRowRun(schedule([]))).toBeUndefined();
    expect(nextRowRun(schedule([{ next_run: null, cron: "0 3 * * *" }]))).toBeUndefined();
  });

  it("picks the soonest, whatever the order", () => {
    const next = nextRowRun(
      schedule([
        { next_run: "2026-10-11T03:00:00Z", cron: "late" },
        { next_run: null, cron: "paused" },
        { next_run: "2026-10-10T02:30:00Z", cron: "soon" },
        { next_run: "2026-10-12T00:00:00Z", cron: "later" },
      ]),
    );
    expect(next).toEqual({ at: "2026-10-10T02:30:00Z", cron: "soon" });
  });

  it("compares instants, not strings, across timezones", () => {
    const next = nextRowRun(
      schedule([
        { next_run: "2026-10-10T05:00:00+10:00", cron: "early-utc" },
        { next_run: "2026-10-10T01:00:00Z", cron: "later-utc" },
      ]),
    );
    expect(next?.cron).toBe("early-utc");
  });
});
