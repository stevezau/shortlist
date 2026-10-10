import { describe, expect, it } from "vitest";
import { latestFinishedRun, latestRunChain, nextRowRun } from "@/lib/dashboard-status";
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

describe("latestRunChain", () => {
  type Over = Partial<Pick<Run, "trigger" | "dry_run" | "status">> & {
    began?: string | null;
    users_ok?: number;
    users_error?: number;
  };
  const sched = (id: number, started: string, finished: string, over: Over = {}) =>
    ({
      id,
      status: over.status ?? "ok",
      trigger: over.trigger ?? "schedule",
      dry_run: over.dry_run ?? false,
      started_at: started,
      began_at: over.began === undefined ? started : over.began,
      finished_at: finished,
      privacy: null,
      stats: { users_ok: over.users_ok ?? 0, users_error: over.users_error ?? 0 },
    }) as unknown as Run;

  const night = {
    big: sched(12, "2026-10-09T02:30:00Z", "2026-10-09T05:10:00Z", { users_ok: 46 }),
    queued: sched(13, "2026-10-09T03:30:00Z", "2026-10-09T05:12:00Z", { began: "2026-10-09T05:10:00Z", users_ok: 0 }),
  };

  it("is undefined when nothing has finished", () => {
    expect(latestRunChain([])).toBeUndefined();
  });

  it("joins two overlapping scheduled runs and links the one that did the work", () => {
    const chain = latestRunChain([night.queued, night.big]);
    expect(chain?.runs.map((r) => r.id)).toEqual([13, 12]);
    expect(chain?.people).toBe(46);
    expect(chain?.linkRun.id).toBe(12);
    expect(chain?.finishedAt).toBe("2026-10-09T05:12:00Z");
    expect(chain?.elapsedMs).toBe((2 * 60 + 42) * 60 * 1000);
  });

  it("does not chain a run from the previous night", () => {
    const prev = sched(11, "2026-10-08T02:30:00Z", "2026-10-08T05:00:00Z", { users_ok: 40 });
    const chain = latestRunChain([night.queued, night.big, prev]);
    expect(chain?.runs.map((r) => r.id)).toEqual([13, 12]);
  });

  it("does not chain a dry run, and a dry latest run stands alone", () => {
    const dry = sched(11, "2026-10-09T02:00:00Z", "2026-10-09T04:00:00Z", { dry_run: true, users_ok: 5 });
    expect(latestRunChain([night.queued, dry])?.runs.map((r) => r.id)).toEqual([13]);
    const dryLatest = sched(14, "2026-10-09T05:00:00Z", "2026-10-09T05:20:00Z", { dry_run: true });
    const alone = latestRunChain([dryLatest, night.queued, night.big]);
    expect(alone?.runs.map((r) => r.id)).toEqual([14]);
    expect(alone?.dryRun).toBe(true);
  });

  it("does not chain a manual latest run", () => {
    const manual = sched(14, "2026-10-09T05:00:00Z", "2026-10-09T05:20:00Z", { trigger: "manual" });
    expect(latestRunChain([manual, night.queued, night.big])?.runs).toHaveLength(1);
  });

  it("is an error when either run failed", () => {
    const failedBig = sched(12, "2026-10-09T02:30:00Z", "2026-10-09T05:10:00Z", { status: "error", users_ok: 40, users_error: 6 });
    const chain = latestRunChain([night.queued, failedBig]);
    expect(chain?.health.tone).toBe("error");
    expect(chain?.failed).toBe(6);
    const failedLate = sched(13, "2026-10-09T03:30:00Z", "2026-10-09T05:12:00Z", { status: "error" });
    expect(latestRunChain([failedLate, night.big])?.health.tone).toBe("error");
  });

  it("links a lone run to itself", () => {
    expect(latestRunChain([night.big])?.linkRun.id).toBe(12);
  });
});
