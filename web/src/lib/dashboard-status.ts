import { runElapsedMs } from "@/lib/format";
import { runHealth, type RunHealth } from "@/lib/run-status";
import type { Run, ScheduleResponse } from "@/lib/types";

/** The newest run that finished as OK or Failed — the same run `/api/report`'s `last_status` reads. */
export function latestFinishedRun(runs: Run[] | undefined): Run | undefined {
  return runs?.find((run) => run.status === "ok" || run.status === "error");
}

/** One night's scheduled runs summed up: two cron groups can queue behind each other, and the
 *  later, near-empty run would otherwise stand in for the whole night. */
export type RunChain = {
  runs: Run[];
  /** The run the summary links to: the only run, or the one that processed the most people. */
  linkRun: Run;
  health: RunHealth;
  people: number;
  failed: number;
  elapsedMs: number | null;
  finishedAt: string | null;
  dryRun: boolean;
};

const HEALTH_RANK: Record<RunHealth["tone"], number> = { error: 3, warn: 2, ok: 1, neutral: 0 };

const ms = (iso: string | null | undefined): number => (iso ? Date.parse(iso) : Number.NaN);
const beganMs = (run: Run): number => ms(run.began_at ?? run.started_at);

/**
 * The latest finished run, together with the finished scheduled runs it overlapped or waited behind.
 *
 * Only a scheduled, non-dry latest run chains; a manual or dry one stands alone. An earlier run joins
 * when it finished at or after the chain's earliest start (it was still running, or this run was
 * queued behind it), so last night's run is never joined by the previous night's.
 */
export function latestRunChain(runs: Run[] | undefined): RunChain | undefined {
  const latest = latestFinishedRun(runs);
  if (!latest) return undefined;
  const chain = [latest];
  if (latest.trigger === "schedule" && !latest.dry_run) {
    let earliestStart = ms(latest.started_at);
    const earlier = (runs ?? [])
      .filter(
        (run) =>
          run !== latest &&
          (run.status === "ok" || run.status === "error") &&
          run.trigger === "schedule" &&
          !run.dry_run &&
          run.finished_at,
      )
      .sort((a, b) => ms(b.finished_at) - ms(a.finished_at));
    for (const run of earlier) {
      if (ms(run.finished_at) < earliestStart) continue;
      chain.push(run);
      earliestStart = Math.min(earliestStart, ms(run.started_at));
    }
  }

  const health = chain.map((run) => runHealth(run)).reduce((worst, h) => (HEALTH_RANK[h.tone] > HEALTH_RANK[worst.tone] ? h : worst));
  const people = (run: Run) => run.stats.users_ok ?? 0;
  const began = Math.min(...chain.map(beganMs));
  const lastFinisher = chain.reduce((best, run) => (ms(run.finished_at) > ms(best.finished_at) ? run : best));
  const finished = ms(lastFinisher.finished_at);
  return {
    runs: chain,
    // The run that did the work is the one worth opening; a later run that only found everyone
    // already built has nothing to show. Ties keep the newest.
    linkRun: chain.reduce((best, run) => (people(run) > people(best) ? run : best)),
    health,
    people: chain.reduce((sum, run) => sum + people(run), 0),
    failed: chain.reduce((sum, run) => sum + (run.stats.users_error ?? 0), 0),
    elapsedMs: runElapsedMs(
      Number.isNaN(began) ? null : new Date(began).toISOString(),
      Number.isNaN(finished) ? null : new Date(finished).toISOString(),
    ),
    finishedAt: lastFinisher.finished_at,
    dryRun: latest.dry_run,
  };
}

/** The soonest a row next builds. Jobs are left out: the privacy sync fires every 30 minutes and is
 *  not a run, so "next run in 12m" off it would be wrong about the thing the owner is asking. */
export function nextRowRun(schedule: ScheduleResponse | undefined): { at: string; cron: string } | undefined {
  let next: { at: string; cron: string } | undefined;
  for (const group of schedule?.rows ?? []) {
    if (!group.next_run) continue;
    if (!next || Date.parse(group.next_run) < Date.parse(next.at)) next = { at: group.next_run, cron: group.cron };
  }
  return next;
}
