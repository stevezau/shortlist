import { useQuery } from "@tanstack/react-query";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { jobStatusLabel, timeAgo } from "@/lib/format";
import { isActiveJob, jobDuration, jobStatusTone } from "@/lib/job-status";
import type { Job } from "@/lib/types";

/** What it was asked to do, what came back, and why it failed — so a failure is diagnosable here
 *  rather than in the container log. */
export function JobDetail({ job }: { job: Job }) {
  const rows: [string, string][] = [];
  if (Object.keys(job.payload ?? {}).length) {
    rows.push(["Asked to", JSON.stringify(job.payload)]);
  }
  for (const [key, value] of Object.entries(job.result ?? {})) {
    if (key === "detail") continue; // already the summary line
    const rendered = Array.isArray(value) ? value.join(", ") : String(value);
    if (rendered) rows.push([key, rendered]);
  }
  if (job.started_at) {
    rows.push(["Started", new Date(job.started_at).toLocaleString()]);
  }
  const took = jobDuration(job);
  if (took) rows.push(["Took", took]);
  rows.push(["Attempts", `${job.attempts} of ${job.max_attempts}`]);

  return (
    <div className="space-y-2 border-t bg-muted/20 px-4 py-3 text-sm">
      {job.error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive-text">
          {job.error}
        </p>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className="break-all font-mono text-xs">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** One expandable row per past run of a single job kind. */
function HistoryRow({ job, first }: { job: Job; first: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={first ? "" : "border-t"}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-2 text-left text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <ChevronRight
          aria-hidden="true"
          className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
        />
        {job.status === "failed" && (
          <TriangleAlert
            aria-hidden="true"
            className="size-4 shrink-0 text-destructive-text"
          />
        )}
        {job.status === "running" && (
          <span
            aria-hidden="true"
            className="size-2 shrink-0 animate-pulse rounded-full bg-primary"
          />
        )}
        <span
          className={`w-40 shrink-0 truncate ${jobStatusTone(job.status)}`}
          data-testid="history-status"
        >
          {jobStatusLabel(job)}
        </span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">
          {job.detail || job.error || "—"}
        </span>
        {jobDuration(job) && (
          <span className="hidden shrink-0 tabular-nums text-muted-foreground sm:block">
            {jobDuration(job)}
          </span>
        )}
        <span className="shrink-0 whitespace-nowrap text-muted-foreground">
          {job.created_at ? timeAgo(job.created_at) : "—"}
        </span>
      </button>
      {open && <JobDetail job={job} />}
    </div>
  );
}

/**
 * Every previous run of ONE job kind.
 *
 * Fetched per-kind and only once the operator opens it: a flat "last 25 jobs across all kinds" list
 * could not answer "has the roster sync been failing?" at all — a single noisy kind pushed every
 * other one off the end of it.
 */
export function JobHistory({ kind }: { kind: string }) {
  const jobs = useQuery({
    queryKey: ["jobs", kind],
    queryFn: () => api.getJobs(kind, 50),
    refetchInterval: (query) =>
      (query.state.data ?? []).some(isActiveJob) ? 3_000 : false,
  });

  if (jobs.isPending) return <Skeleton className="h-16 w-full" />;
  if (jobs.isError) {
    return (
      <p role="alert" className="px-1 py-2 text-sm text-destructive-text">
        Couldn&rsquo;t load this job&rsquo;s history.{" "}
        <button
          type="button"
          className="underline underline-offset-4"
          onClick={() => jobs.refetch()}
        >
          Try again
        </button>
      </p>
    );
  }
  if (jobs.data.length === 0) {
    return (
      <p className="px-1 py-2 text-sm text-muted-foreground">
        This job hasn&rsquo;t run yet.
      </p>
    );
  }
  return (
    <div className="overflow-hidden rounded-md border">
      {jobs.data.map((job, index) => (
        <HistoryRow key={job.id} job={job} first={index === 0} />
      ))}
    </div>
  );
}
