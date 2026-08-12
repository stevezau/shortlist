import { describe, expect, it } from "vitest";

import { isActiveJob, jobDuration, jobStatusTone } from "@/lib/job-status";
import type { Job } from "@/lib/types";

function job(patch: Partial<Job> & { id: number }): Job {
  return {
    kind: "sync.users",
    status: "done",
    attempts: 1,
    max_attempts: 3,
    detail: "",
    error: null,
    payload: {},
    result: {},
    created_at: "2026-08-12T10:00:00Z",
    started_at: "2026-08-12T10:00:01Z",
    finished_at: "2026-08-12T10:00:05Z",
    ...patch,
  };
}

describe("jobDuration", () => {
  it("reports how long a finished job took", () => {
    expect(jobDuration(job({ id: 1, status: "done" }))).toBe("4.0s");
    expect(jobDuration(job({ id: 2, status: "failed" }))).toBe("4.0s");
  });

  // The bug this gate exists for: `_finish` stamps `finished_at` and puts a failed job back to
  // `queued` WITHOUT clearing `started_at`, because `finished_at` is also the backoff clock. Both
  // timestamps are therefore present, positive, and describe the attempt before this one — so a job
  // that has not started rendered "Retrying (attempt 1) · 4.0s".
  it("reports nothing for a retry waiting out its backoff", () => {
    expect(
      jobDuration(job({ id: 3, status: "queued", attempts: 1 })),
    ).toBeNull();
  });

  it("reports nothing for a job that has never started", () => {
    expect(
      jobDuration(
        job({
          id: 4,
          status: "queued",
          attempts: 0,
          started_at: null,
          finished_at: null,
        }),
      ),
    ).toBeNull();
  });

  it("reports nothing while a job is still running", () => {
    expect(
      jobDuration(job({ id: 5, status: "running", finished_at: null })),
    ).toBeNull();
    // A running RETRY carries the previous attempt's `finished_at`, which predates the new
    // `started_at` — a negative span, and still not this attempt's duration.
    expect(
      jobDuration(
        job({
          id: 6,
          status: "running",
          attempts: 2,
          started_at: "2026-08-12T10:05:00Z",
          finished_at: "2026-08-12T10:00:05Z",
        }),
      ),
    ).toBeNull();
  });

  it("scales the unit to the span", () => {
    expect(
      jobDuration(job({ id: 7, finished_at: "2026-08-12T10:00:01.400Z" })),
    ).toBe("400ms");
    expect(
      jobDuration(job({ id: 8, finished_at: "2026-08-12T10:02:01Z" })),
    ).toBe("2m");
  });
});

describe("job status helpers", () => {
  it("counts queued and running as active", () => {
    expect(isActiveJob(job({ id: 1, status: "queued" }))).toBe(true);
    expect(isActiveJob(job({ id: 2, status: "running" }))).toBe(true);
    expect(isActiveJob(job({ id: 3, status: "done" }))).toBe(false);
  });

  it("only colours a failure", () => {
    expect(jobStatusTone("failed")).toContain("destructive");
    expect(jobStatusTone("done")).not.toContain("destructive");
  });
});
