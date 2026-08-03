import type { Job } from "@/lib/types";

/** How long a job took, or null while it is still in flight. */
export function jobDuration(job: Job): string | null {
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
