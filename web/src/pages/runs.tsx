import { Loader2, Play } from "lucide-react";
import { Link } from "react-router-dom";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDate, runStatusVariant, timeAgo } from "@/lib/format";
import { useRuns, useStartRun } from "@/lib/queries";
import type { Run } from "@/lib/types";

function RunsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

function RunRow({ run }: { run: Run }) {
  return (
    <TableRow>
      <TableCell>
        <Link
          to={`/runs/${run.id}`}
          className="rounded-sm font-medium hover:underline"
        >
          #{run.id}
        </Link>
      </TableCell>
      <TableCell className="text-muted-foreground">{run.trigger}</TableCell>
      <TableCell
        className="text-muted-foreground"
        title={formatDate(run.started_at)}
      >
        {timeAgo(run.started_at)}
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          <Badge variant={runStatusVariant(run.status)}>{run.status}</Badge>
          {run.dry_run && <Badge variant="outline">dry-run</Badge>}
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground">
        {run.stats.users_ok} ok
        {run.stats.users_error > 0 && (
          <span className="text-destructive">
            {" "}
            · {run.stats.users_error} failed
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}

export function RunsPage() {
  const runsQuery = useRuns();
  const startRun = useStartRun();

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
          <p className="text-sm text-muted-foreground">
            Every time Rowarr rebuilt rows, and how it went.
          </p>
        </div>
        <Button
          onClick={() => startRun.mutate({})}
          disabled={startRun.isPending}
        >
          {startRun.isPending ? (
            <Loader2 className="animate-spin" aria-hidden="true" />
          ) : (
            <Play aria-hidden="true" />
          )}
          Run all users now
        </Button>
      </header>

      <QueryBoundary
        query={runsQuery}
        skeleton={<RunsSkeleton />}
        isEmpty={(runs) => runs.length === 0}
        empty={
          <EmptyState
            title="No runs yet"
            hint="Rowarr hasn't built any rows so far. Start one with the button above, or wait for the nightly schedule."
          />
        }
      >
        {(runs) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run</TableHead>
                <TableHead>Trigger</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Users</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
            </TableBody>
          </Table>
        )}
      </QueryBoundary>
    </div>
  );
}
