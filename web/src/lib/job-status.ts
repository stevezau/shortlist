import type { Job } from "@/lib/types";

/** How long a job took, or null unless it has actually finished.
 *
 * Gated on a TERMINAL status, not just on the two timestamps being present. A retry keeps both:
 * `_finish` stamps `finished_at` and puts the job back to `queued` without clearing `started_at`
 * (server `jobs.py`, where `finished_at` doubles as the backoff clock), so a job waiting out its
 * backoff carries a complete, positive-looking pair belonging to the attempt BEFORE this one — and
 * rendered it as "Retrying (attempt 1) · 4.2s", a duration for work that has not started.
 */
export function jobDuration(job: Job): string | null {
  if (job.status !== "done" && job.status !== "failed") return null;
  if (!job.started_at || !job.finished_at) return null;
  const ms = Date.parse(job.finished_at) - Date.parse(job.started_at);
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}

/** `queued` WITH attempts is a retry in flight, not work that has yet to start. */
export function isActiveJob(job: Job): boolean {
  return job.status === "running" || job.status === "queued";
}

export function jobStatusTone(status: Job["status"]): string {
  if (status === "failed") return "text-destructive-text";
  if (status === "done") return "text-muted-foreground";
  return "text-foreground";
}
