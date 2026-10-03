import type { Run, ScheduleResponse } from "@/lib/types";

/** The newest run that finished as OK or Failed — the same run `/api/report`'s `last_status` reads. */
export function latestFinishedRun(runs: Run[] | undefined): Run | undefined {
  return runs?.find((run) => run.status === "ok" || run.status === "error");
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
