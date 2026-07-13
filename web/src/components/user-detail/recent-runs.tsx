import { Link } from "react-router-dom";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, runStatusVariant } from "@/lib/format";
import { useUserRuns } from "@/lib/queries";

/** This person's recent runs, each with a diff summary. All four states via QueryBoundary. */
export function RecentRuns({ userId }: { userId: number }) {
  const query = useUserRuns(userId);
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-32 w-full" />}
      isEmpty={(runs) => runs.length === 0}
      empty={
        <EmptyState
          title="No runs yet"
          hint="Once a run processes this person, each one shows up here with what it changed and why."
        />
      }
    >
      {(runs) => (
        <ul className="space-y-2">
          {runs.map((run) => {
            const added = run.diff.added?.length ?? 0;
            const removed = run.diff.removed?.length ?? 0;
            return (
              <li key={run.run_id} className="rounded-md border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <Link
                      to={`/runs/${run.run_id}`}
                      className="font-medium hover:text-primary hover:underline"
                    >
                      Run #{run.run_id}
                    </Link>
                    <Badge variant={runStatusVariant(run.status)}>
                      {run.status}
                    </Badge>
                    {run.dry_run && <Badge variant="outline">dry-run</Badge>}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {run.finished_at
                      ? formatDate(run.finished_at)
                      : formatDate(run.started_at)}
                  </span>
                </div>
                {run.error ? (
                  <p className="mt-1 font-mono text-xs text-destructive">
                    {run.error}
                  </p>
                ) : (
                  <p className="mt-1 text-xs text-muted-foreground">
                    +{added} added · −{removed} removed · {run.picks.length}{" "}
                    picks
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </QueryBoundary>
  );
}
