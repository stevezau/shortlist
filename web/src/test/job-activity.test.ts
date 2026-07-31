import { describe, expect, it } from "vitest";

import {
  isInFlight,
  jobTransitions,
  queuedReason,
  statusMap,
} from "@/lib/job-activity";
import type { Job } from "@/lib/types";

function job(patch: Partial<Job> & { id: number }): Job {
  return {
    kind: "user.cleanup",
    status: "queued",
    attempts: 0,
    max_attempts: 3,
    detail: "",
    error: null,
    payload: {},
    result: {},
    // `JobOut.created_at` is not nullable — the row is stamped on insert. The fixture used to say
    // null, which the `as Job` cast hid.
    created_at: "2026-07-28T10:00:00Z",
    started_at: null,
    finished_at: null,
    ...patch,
  };
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

  it("announces a SUCCESS it never saw start either", () => {
    // The idle poll is 30s and most jobs finish in under a second, so this is the COMMON case, not an
    // edge one: you disable someone, the cleanup runs instantly, and the next poll is the first time
    // the queue is read. Requiring a prior sighting made that path silent while failures still spoke
    // — so the feedback was missing exactly when the work had gone fine.
    const { started, finished } = jobTransitions(new Map(), [
      job({ id: 9, status: "done", detail: "Removed 2 rows for mike" }),
    ]);
    expect(finished.map((j) => j.id)).toEqual([9]);
    expect(started, "a finished job must not also announce a start").toEqual(
      [],
    );
  });

  it("reports an outcome once, and never as a start", () => {
    // First-load silence is NOT this function's job — the caller passes `null` for its very first poll
    // and returns early (asserted in activity-indicator.test.tsx). Making this function silent for an
    // unseen job as well looked like the same rule but was a second, wrong one: every job that
    // finished inside one 30s idle window went unannounced.
    const { started, finished } = jobTransitions(new Map(), [
      job({ id: 1, status: "done" }),
      job({ id: 2, status: "done" }),
    ]);
    expect(started).toEqual([]);
    expect(finished.map((j) => j.id)).toEqual([1, 2]);
  });
});

describe("queuedReason", () => {
  it("blames the run when a Plex-writing job is queued while one is active", () => {
    const reason = queuedReason(true, true);
    expect(reason.text).toMatch(/waiting for the run to finish/i);
    expect(reason.title).toMatch(/run is writing to plex/i);
  });

  it("does not blame a run when a Plex-writing job is queued and none is active", () => {
    // Serialized behind another WRITER job, or simply about to be claimed — either way, not the run.
    const reason = queuedReason(true, false);
    expect(reason.text).toMatch(/waiting its turn/i);
    expect(reason.text).not.toMatch(/run/i);
  });

  it("gives a read-only job the parallelism-cap reason, never the run — even while one is active", () => {
    // Only writers wait on a run (services/jobs.py::_claimable); a read-only job queued during a run
    // is queued for the reader cap, and telling it "a run is blocking you" would be a guess, not a fact.
    const reason = queuedReason(false, true);
    expect(reason.text).toMatch(/waiting for a free slot/i);
    expect(reason.text).not.toMatch(/run/i);
    // "jobs run at once" is fine — it's "a/the run" (the engine run) this must never say.
    expect(reason.title).not.toMatch(/\b(a|the) run\b/i);
  });
});
