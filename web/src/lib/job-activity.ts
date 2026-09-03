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
    // `excludeRoutine`: the header is a feed of things worth telling you about, so the high-volume
    // automatic kinds are dropped by the SERVER (see `JobKind.routine`) rather than filtered here —
    // one per playback stop measured 165 of the 197 jobs queued in a day on a 46-user server, which
    // is more than this page holds, so a client-side filter would have shown an empty list. Coming
    // out of the poll entirely is also what silences their toasts: they never reach `jobTransitions`.
    // Failures are exempt server-side, so a reconcile that fails still toasts and still counts.
    queryFn: () => api.getJobs(undefined, ACTIVITY_LIMIT, undefined, true),
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

/**
 * Is an engine run writing to Plex right now? `_plex_busy` (services/jobs.py) is what actually
 * defers a Plex-writing job while a run holds the writer lock — this is the SPA's one window onto
 * that same fact: the newest run row, which carries `status: "running"` for exactly as long as the
 * lock is held. Polled only while `enabled` (a queued job that might be waiting on it exists), so
 * an idle popover costs nothing extra.
 */
export function useRunActive(enabled: boolean): boolean {
  const query = useQuery({
    queryKey: ["runs", "active"],
    queryFn: () => api.getRuns(undefined, undefined, 1),
    enabled,
    refetchInterval: enabled ? 3_000 : false,
  });
  return query.data?.[0]?.status === "running";
}

/**
 * Does this job kind write to Plex or plex.tv? `GET /api/schedule` is the one endpoint that carries
 * `writes_plex` per kind (services/jobs.py's `JobKind.writes_plex`) — but it only lists the kinds
 * that run on a timer, so the four kinds queued automatically by a mutation (`user.cleanup`,
 * `user.hide`, `user.restore`, `row.reconcile`) are absent from it and fall back to `true` here.
 * That mirrors the server's own default for a kind it doesn't recognise
 * (`services/jobs.py::writes_plex`): the safe assumption is "takes the lock", never "assume it's
 * harmless".
 */
export function useWritesPlex(): (kind: string) => boolean {
  const query = useQuery({
    queryKey: queryKeys.schedule,
    queryFn: api.getSchedule,
    staleTime: 5 * 60_000,
  });
  const map = new Map<string, boolean>();
  for (const entry of query.data?.jobs ?? []) {
    if (entry.kind && entry.writes_plex !== undefined) {
      map.set(entry.kind, entry.writes_plex);
    }
  }
  return (kind: string) => map.get(kind) ?? true;
}

/**
 * Why a queued job hasn't started, in words that say what it's waiting FOR — the gap that made
 * "waiting" alone unreadable: it never said whether the wait was expected or something was stuck.
 *
 * Only a job that writes to Plex ever waits on a RUN (`services/jobs.py::_claimable`) — a read-only
 * job that is queued is queued for a different reason (the parallel-reader cap), so it gets its own
 * wording rather than being told a run is holding it up.
 */
export function queuedReason(
  writesPlex: boolean,
  runActive: boolean,
): { text: string; title: string } {
  if (!writesPlex) {
    return {
      text: "waiting for a free slot",
      title:
        "Shortlist limits how many background jobs run at once. This starts as soon as one of them finishes.",
    };
  }
  if (runActive) {
    return {
      text: "waiting for the run to finish",
      title:
        "A run is writing to Plex right now, and Shortlist never writes to Plex from two places at once — this starts the moment the run finishes.",
    };
  }
  return {
    text: "waiting its turn",
    title:
      "Shortlist writes to Plex one thing at a time, so this starts as soon as the current write finishes.",
  };
}
