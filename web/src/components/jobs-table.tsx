import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Cog, TriangleAlert } from "lucide-react";
import { useState } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { jobStatusLabel, timeAgo } from "@/lib/format";
import type { Job } from "@/lib/types";

/** Plain English — `sync.check` means nothing to someone reading their own server's history. */
const KIND_LABELS: Record<string, string> = {
  "sync.check": "Sync check",
  "privacy.sync": "Privacy sync",
  "user.cleanup": "Remove a disabled user's rows",
  "user.hide": "Hide a paused user's rows",
  "row.reconcile": "Tidy up after a row change",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

type Filter = "all" | "active" | "failed";

/** `queued` WITH attempts is a retry in flight, not work that has yet to start. */
function isActive(job: Job): boolean {
  return job.status === "running" || job.status === "queued";
}

function statusTone(job: Job): string {
  if (job.status === "failed") return "text-destructive";
  if (isActive(job)) return "text-foreground";
  return "text-muted-foreground";
}

function duration(job: Job): string | null {
  if (!job.started_at || !job.finished_at) return null;
  const ms = Date.parse(job.finished_at) - Date.parse(job.started_at);
  if (!Number.isFinite(ms) || ms < 0) return null;
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** What it was asked to do, what came back, and why it failed — so a failure is diagnosable here
 *  rather than in the container log. */
function JobDetail({ job }: { job: Job }) {
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
  const took = duration(job);
  if (took) rows.push(["Took", took]);
  rows.push(["Attempts", `${job.attempts} of ${job.max_attempts}`]);

  return (
    <div className="space-y-2 border-t bg-muted/20 px-4 py-3 text-sm">
      {job.error && (
        <p className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-destructive">
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

/**
 * Background maintenance history.
 *
 * Polls only while something is in flight, then stops — a job triggered from the buttons above
 * appears and completes without a reload, but an idle page isn't hitting the API forever.
 */
export function JobsTable() {
  const [filter, setFilter] = useState<Filter>("all");
  const [openId, setOpenId] = useState<number | null>(null);

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.getJobs,
    refetchInterval: (query) =>
      (query.state.data ?? []).some(isActive) ? 3_000 : false,
  });

  const all = jobs.data ?? [];
  const active = all.filter(isActive).length;
  const failed = all.filter((j) => j.status === "failed").length;
  const shown =
    filter === "all"
      ? all
      : filter === "active"
        ? all.filter(isActive)
        : all.filter((j) => j.status === "failed");

  return (
    <section aria-labelledby="jobs-heading" className="mt-10 space-y-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2
            id="jobs-heading"
            className="flex items-center gap-2 text-lg font-semibold"
          >
            <Cog aria-hidden="true" className="size-5 text-muted-foreground" />
            Background jobs
            {active > 0 && (
              <span className="inline-flex items-center gap-1.5 rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary">
                <span className="size-1.5 animate-pulse rounded-full bg-primary" />
                {active} running
              </span>
            )}
          </h2>
          <p className="text-sm text-muted-foreground">
            Maintenance Shortlist ran on its own &mdash; tidying rows after
            someone was disabled, writing share filters when a new person
            appeared, and sync checks. Anything that fails is retried
            automatically.
          </p>
        </div>
        {all.length > 0 && (
          <Segmented<Filter>
            ariaLabel="Filter jobs"
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: `All ${all.length}` },
              { value: "active", label: `Active ${active}` },
              { value: "failed", label: `Failed ${failed}` },
            ]}
          />
        )}
      </div>

      <QueryBoundary
        query={jobs}
        skeleton={<Skeleton className="h-24 w-full" />}
      >
        {(rows) =>
          rows.length === 0 ? (
            // An empty state has to say WHY, or a working feature reads as a broken one.
            <p className="rounded-md border border-dashed bg-muted/30 p-4 text-sm text-muted-foreground">
              Nothing yet. Jobs appear here when Shortlist has maintenance to do
              &mdash; after you disable someone, when a new person gets access
              to your server, or when you run a sync check above. An empty list
              means there has been nothing to fix.
            </p>
          ) : shown.length === 0 ? (
            <p className="rounded-md border border-dashed bg-muted/30 p-4 text-sm text-muted-foreground">
              No {filter} jobs.{" "}
              {filter === "failed" &&
                "Nothing has given up — that's the good outcome."}
            </p>
          ) : (
            <div className="overflow-hidden rounded-md border">
              {shown.map((job, index) => {
                const open = openId === job.id;
                return (
                  <div key={job.id} className={index ? "border-t" : ""}>
                    <button
                      type="button"
                      onClick={() => setOpenId(open ? null : job.id)}
                      aria-expanded={open}
                      className="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
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
                      <span className="min-w-0 flex-1 truncate font-medium">
                        {kindLabel(job.kind)}
                      </span>
                      <span className={`shrink-0 ${statusTone(job)}`}>
                        {jobStatusLabel(job)}
                      </span>
                      <span className="hidden min-w-0 flex-1 truncate text-muted-foreground sm:block">
                        {job.detail}
                      </span>
                      <span className="shrink-0 whitespace-nowrap text-muted-foreground">
                        {job.created_at ? timeAgo(job.created_at) : "—"}
                      </span>
                    </button>
                    {open && <JobDetail job={job} />}
                  </div>
                );
              })}
            </div>
          )
        }
      </QueryBoundary>
    </section>
  );
}
