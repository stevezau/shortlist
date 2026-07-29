import { describe, expect, it } from "vitest";

import { isInFlight, jobTransitions, statusMap } from "@/lib/job-activity";
import type { Job } from "@/lib/types";

function job(patch: Partial<Job> & { id: number }): Job {
  return {
    kind: "user.cleanup",
    status: "queued",
    attempts: 0,
    max_attempts: 3,
    detail: "",
    error: null,
    created_at: null,
    started_at: null,
    finished_at: null,
    ...patch,
  } as Job;
}

describe("job activity", () => {
  it("counts queued and running as in flight", () => {
    expect(isInFlight(job({ id: 1, status: "queued" }))).toBe(true);
    expect(isInFlight(job({ id: 2, status: "running" }))).toBe(true);
    expect(isInFlight(job({ id: 3, status: "done" }))).toBe(false);
    expect(isInFlight(job({ id: 4, status: "failed" }))).toBe(false);
  });

  it("announces a job the first time it is seen in flight", () => {
    const { started } = jobTransitions(new Map(), [
      job({ id: 1, status: "queued" }),
    ]);
    expect(started.map((j) => j.id)).toEqual([1]);
  });

  it("says nothing twice for a job that has not changed", () => {
    const jobs = [job({ id: 1, status: "running" })];
    const seen = statusMap(jobs);
    const { started, finished, failed } = jobTransitions(seen, jobs);
    expect([...started, ...finished, ...failed]).toEqual([]);
  });

  it("announces the outcome when a job it was watching finishes", () => {
    const before = statusMap([job({ id: 1, status: "running" })]);
    const { finished, failed } = jobTransitions(before, [
      job({ id: 1, status: "done" }),
    ]);
    expect(finished.map((j) => j.id)).toEqual([1]);
    expect(failed).toEqual([]);
  });

  it("announces a failure even for a job it never saw start", () => {
    // A short job can queue, run and fail entirely between two polls. Silence there would hide the
    // one outcome that actually needs saying.
    const { failed } = jobTransitions(new Map(), [
      job({ id: 9, status: "failed", error: "Plex is down" }),
    ]);
    expect(failed.map((j) => j.id)).toEqual([9]);
  });

  it("does not announce history it has only just loaded", () => {
    // On first load the poll returns everything recent. Announcing a completed job from yesterday
    // would be a wall of toasts for work nobody just did — the caller seeds and stays quiet, and
    // this is the shape that makes that possible.
    const { started, finished } = jobTransitions(new Map(), [
      job({ id: 1, status: "done" }),
      job({ id: 2, status: "done" }),
    ]);
    expect(started).toEqual([]);
    expect(finished).toEqual([]);
  });
});
