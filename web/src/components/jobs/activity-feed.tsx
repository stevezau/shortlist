import { useQuery } from "@tanstack/react-query";
import { ChevronRight, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { JobDetail } from "@/components/jobs/job-history";
import { QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { jobStatusLabel, timeAgo } from "@/lib/format";
import { isActiveJob, jobDuration, jobStatusTone } from "@/lib/job-status";
import type { Job, JobCatalogEntry } from "@/lib/types";

type Filter = "all" | "failed" | "active";

function ActivityRow({
  job,
  label,
  first,
}: {
  job: Job;
  label: string;
  first: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={first ? "" : "border-t"}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
      >
        <ChevronRight
          aria-hidden="true"
          className={`size-4 shrink-0 text-muted-foreground transition-transform ${open ? "rotate-90" : ""}`}
        />
        {job.status === "failed" && (
          <TriangleAlert
            aria-hidden="true"
            className="size-4 shrink-0 text-destructive"
          />
        )}
        {job.status === "running" && (
          <span
            aria-hidden="true"
            className="size-2 shrink-0 animate-pulse rounded-full bg-primary"
          />
        )}
        {/* Which JOB this was, in the same words as the Jobs tab — the old flat table showed the raw
            kind (`sync.check`), which means nothing to someone reading their own server's history. */}
        <span className="w-36 shrink-0 truncate font-medium lg:w-48">
          {label}
        </span>
        {/* w-44, not w-32: the longest status ("Failed after 3 attempts", 146px) was clipped to
            "Failed after 3 atte…" at EVERY width — the one column here whose text must survive. */}
        <span className={`w-44 shrink-0 truncate ${jobStatusTone(job.status)}`}>
          {jobStatusLabel(job)}
        </span>
        {/* Detail and duration only from `lg`: below it, five columns plus a 240px nav rail leave the
            detail ~30px, which truncates to nothing while pushing the row past the viewport. */}
        <span className="hidden min-w-0 flex-1 truncate text-muted-foreground lg:block">
          {job.detail || job.error || "—"}
        </span>
        {jobDuration(job) && (
          <span className="ml-auto hidden shrink-0 tabular-nums text-muted-foreground lg:ml-0 lg:block">
            {jobDuration(job)}
          </span>
        )}
        {/* `ml-auto` keeps the time hard right on the narrow layout, where the flex-1 detail that
            normally does the pushing is hidden. */}
        <span className="ml-auto shrink-0 whitespace-nowrap text-muted-foreground lg:ml-0">
          {job.created_at ? timeAgo(job.created_at) : "—"}
        </span>
      </button>
      {open && <JobDetail job={job} />}
    </div>
  );
}

/**
 * Every job run on this server, newest first, whatever kind it was.
 *
 * The per-job histories answer "is the roster sync healthy?"; this answers the other question —
 * "what has my server actually been doing?" — which no amount of opening nine separate collapsibles
 * gets you. Both views exist because they are genuinely different questions.
 */
/** How many job rows one page shows, and how many each "Show more" adds. */
const PAGE = 100;

export function ActivityFeed({ catalog }: { catalog: JobCatalogEntry[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  // Every background job the server has ever run lands here, so the list only grows. A flat cap
  // silently hid everything past it — "every background job this server has run" was a promise the
  // view stopped keeping the moment you passed 100. Raise the ceiling on demand instead.
  const [limit, setLimit] = useState(PAGE);
  const jobs = useQuery({
    queryKey: ["jobs", "activity", limit],
    queryFn: () => api.getJobs(undefined, limit),
    // Slow when idle rather than stopping: a feed of "every background job this server has run" that
    // never refetches shows a job appearing only if you happen to reload.
    refetchInterval: (query) =>
      (query.state.data ?? []).some(isActiveJob) ? 3_000 : 15_000,
  });
  // A full page back means there is probably more; a short one means we reached the end.
  const maybeMore = (jobs.data ?? []).length >= limit;

  const labels = Object.fromEntries(catalog.map((e) => [e.kind, e.label]));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Every background job this server has run, newest first. Anything that
          fails is retried automatically.
        </p>
        <Segmented<Filter>
          ariaLabel="Filter activity"
          value={filter}
          onChange={setFilter}
          options={[
            { value: "all", label: "All" },
            { value: "active", label: "In flight" },
            { value: "failed", label: "Failed" },
          ]}
        />
      </div>

      <QueryBoundary
        query={jobs}
        skeleton={<Skeleton className="h-64 w-full" />}
      >
        {(rows) => {
          const shown =
            filter === "all"
              ? rows
              : filter === "active"
                ? rows.filter(isActiveJob)
                : rows.filter((j) => j.status === "failed");
          if (rows.length === 0) {
            // An empty state has to say WHY, or a working feature reads as a broken one.
            return (
              <p className="rounded-md border border-dashed bg-muted/30 p-4 text-sm text-muted-foreground">
                Nothing yet. Jobs appear here when Shortlist has maintenance to
                do &mdash; after you disable someone, when a new person gets
                access to your server, or when you run one from the Jobs tab. An
                empty list means there has been nothing to fix.
              </p>
            );
          }
          if (shown.length === 0) {
            return (
              <p className="rounded-md border border-dashed bg-muted/30 p-4 text-sm text-muted-foreground">
                No {filter === "active" ? "jobs in flight" : "failed jobs"}.
                {filter === "failed" &&
                  " Nothing has given up — that's the good outcome."}
              </p>
            );
          }
          return (
            <>
              <div className="overflow-hidden rounded-md border">
                {shown.map((job, index) => (
                  <ActivityRow
                    key={job.id}
                    job={job}
                    label={labels[job.kind] ?? job.kind}
                    first={index === 0}
                  />
                ))}
              </div>
              {maybeMore && (
                <div className="flex justify-center pt-1">
                  <Button
                    variant="outline"
                    size="sm"
                    loading={jobs.isFetching}
                    onClick={() => setLimit((n) => n + PAGE)}
                  >
                    Show older
                  </Button>
                </div>
              )}
            </>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
