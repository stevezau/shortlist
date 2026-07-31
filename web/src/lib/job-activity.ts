import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import type { Job, JobCatalogEntry } from "@/lib/types";

/** How many recent jobs the activity poll carries. Enough to catch a burst of automatic jobs (one
 *  `user.cleanup` per person on a "disable all") without pulling the whole history every 3 seconds. */
const ACTIVITY_LIMIT = 30;

export function isInFlight(job: Job): boolean {
  return job.status === "queued" || job.status === "running";
}

/**
 * The jobs poll that drives the header's activity badge and its toasts.
 *
 * Deliberately an OBSERVER rather than a notification fired from each enqueue site. Jobs are queued
 * from six different places — disabling a person, pausing one, un-pausing one, changing a row, a new
 * account appearing on the roster, and the manual buttons — and the automatic ones had no
 * enqueue-time feedback at all: you did a thing and nothing visibly happened. Watching the queue
 * covers every path at once, including any added later, which a per-call-site toast could not.
 *
 * Polls fast while something is in flight and slowly otherwise, so an idle server is not woken every
 * three seconds all day.
 */
export function useJobActivity() {
  return useQuery({
    queryKey: ["jobs", "activity"],
    queryFn: () => api.getJobs(undefined, ACTIVITY_LIMIT),
    refetchInterval: (query) => {
      const jobs = query.state.data as Job[] | undefined;
      return jobs?.some(isInFlight) ? 3_000 : 30_000;
    },
  });
}

/** The catalogue, for turning a kind into the label a human recognises. Cached hard — it only
 *  changes when Shortlist itself is upgraded. */
export function useJobLabels() {
  const query = useQuery({
    queryKey: queryKeys.jobsCatalog,
    queryFn: api.getJobCatalog,
    staleTime: 5 * 60_000,
  });
  const labels = new Map(
    (query.data ?? []).map((entry: JobCatalogEntry) => [
      entry.kind,
      entry.label,
    ]),
  );
  return (kind: string) => labels.get(kind) ?? kind;
}

/** What changed between two polls, so the caller can announce it once.
 *
 *  Keyed on job id + status: a job that goes queued → running → done is three transitions of the
 *  same row, and only the ones worth saying out loud are returned. */
export function jobTransitions(
  previous: Map<number, Job["status"]>,
  jobs: Job[],
): { started: Job[]; finished: Job[]; failed: Job[] } {
  const started: Job[] = [];
  const finished: Job[] = [];
  const failed: Job[] = [];
  for (const job of jobs) {
    const before = previous.get(job.id);
    if (before === job.status) continue;
    // A job seen for the first time ALREADY done (the poll landed after it finished) is not worth
    // two toasts — announce the outcome only. It must still be announced: the idle poll is 30s and
    // most jobs finish in under a second, so requiring a prior sighting made the common case silent
    // and left the enqueue with no feedback at all — the exact gap the toasts exist to close.
    if (before === undefined && isInFlight(job)) started.push(job);
    else if (job.status === "done") finished.push(job);
    else if (job.status === "failed") failed.push(job);
  }
  return { started, finished, failed };
}

export function statusMap(jobs: Job[]): Map<number, Job["status"]> {
  return new Map(jobs.map((job) => [job.id, job.status]));
}
