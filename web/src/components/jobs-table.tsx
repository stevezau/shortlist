import { useQuery } from "@tanstack/react-query";
import { Cog, TriangleAlert } from "lucide-react";

import { QueryBoundary } from "@/components/query-boundary";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { jobStatusLabel, timeAgo } from "@/lib/format";

/** Plain-English names — `sync.check` means nothing to someone reading their own server's history. */
const KIND_LABELS: Record<string, string> = {
  "sync.check": "Sync check",
  "privacy.sync": "Privacy sync",
  "user.cleanup": "Remove a disabled user's rows",
};

function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/**
 * Background maintenance history — cleanups, share-filter writes, sync checks.
 *
 * Separate from runs on purpose: a run is a long operation with per-user results and its own page;
 * a job is a short mechanical fix-up. They share this page because "what has this server been
 * doing?" is one question, which is also how Sonarr/Radarr split Tasks from History.
 */
export function JobsTable() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: api.getJobs,
    // Cheap and short-lived, so a job triggered from Tools shows up here without a manual reload.
    refetchInterval: 15_000,
  });

  return (
    <section aria-labelledby="jobs-heading" className="mt-10 space-y-3">
      <div>
        <h2
          id="jobs-heading"
          className="flex items-center gap-2 text-lg font-semibold"
        >
          <Cog aria-hidden="true" className="size-5 text-muted-foreground" />
          Background jobs
        </h2>
        <p className="text-sm text-muted-foreground">
          Maintenance Shortlist ran on its own — tidying rows after someone was
          disabled, writing share filters when a new person appeared, and sync
          checks. Anything that fails is retried automatically.
        </p>
      </div>

      <QueryBoundary
        query={jobs}
        skeleton={<Skeleton className="h-24 w-full" />}
      >
        {(rows) =>
          rows.length === 0 ? (
            // The empty state has to say WHY it's empty, or a working feature reads as a broken one.
            <p className="rounded-md border border-dashed bg-muted/30 p-4 text-sm text-muted-foreground">
              Nothing yet. Jobs appear here when Shortlist has maintenance to do
              — after you disable someone, when a new person gets access to your
              server, or when you run a sync check from{" "}
              <strong className="text-foreground">Tools</strong>. An empty list
              means there has been nothing to fix.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>Job</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>What happened</TableHead>
                    <TableHead>When</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((job) => (
                    <TableRow key={job.id}>
                      <TableCell className="font-medium">
                        {kindLabel(job.kind)}
                      </TableCell>
                      <TableCell>
                        <span className="flex items-center gap-1.5">
                          {job.status === "failed" && (
                            <TriangleAlert
                              aria-hidden="true"
                              className="size-3.5 shrink-0 text-destructive"
                            />
                          )}
                          <span
                            className={
                              job.status === "failed" ? "text-destructive" : ""
                            }
                          >
                            {jobStatusLabel(job)}
                          </span>
                        </span>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {job.error ? (
                          <span className="text-destructive">{job.error}</span>
                        ) : (
                          (job.detail ?? "")
                        )}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-muted-foreground">
                        {job.created_at ? timeAgo(job.created_at) : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )
        }
      </QueryBoundary>
    </section>
  );
}
