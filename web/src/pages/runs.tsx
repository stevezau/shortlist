import { ListChecks, Play } from "lucide-react";
import { Link } from "react-router-dom";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
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
import {
  formatDate,
  runStatusLabel,
  runStatusVariant,
  timeAgo,
  triggerLabel,
} from "@/lib/format";
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
    <TableRow className="group">
      <TableCell>
        <Link
          to={`/runs/${run.id}`}
          className="rounded-sm font-medium tabular-nums group-hover:text-primary group-hover:underline"
        >
          #{run.id}
        </Link>
      </TableCell>
      <TableCell className="text-muted-foreground">
        {triggerLabel(run.trigger)}
      </TableCell>
      <TableCell
        className="text-muted-foreground"
        title={formatDate(run.started_at)}
      >
        {timeAgo(run.started_at)}
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          <Badge variant={runStatusVariant(run.status)}>
            {runStatusLabel(run.status)}
          </Badge>
          {run.dry_run && (
            <Badge
              variant="outline"
              title="A rehearsal — nothing was written to Plex."
            >
              Test run
            </Badge>
          )}
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
    <div>
      <PageHeader
        icon={ListChecks}
        title="Runs"
        subtitle="Every time Shortlist rebuilt rows, and how it went."
        actions={
          <Button
            onClick={() => startRun.mutate({})}
            loading={startRun.isPending}
          >
            {!startRun.isPending && <Play aria-hidden="true" />}
            Run all users now
          </Button>
        }
      />

      {/* The write gate refuses a run in plain English (no passing Privacy Check, PMS too old).
          Swallowing that left the button looking like it had done nothing at all. */}
      {startRun.isError && (
        <MutationAlert
          className="mb-4"
          error={startRun.error}
          fallback="Couldn’t start that run. Check the server log and try again."
        />
      )}

      <QueryBoundary
        query={runsQuery}
        skeleton={<RunsSkeleton />}
        isEmpty={(runs) => runs.length === 0}
        empty={
          <EmptyState
            title="No runs yet"
            hint="Shortlist hasn't built any rows so far. Start one with the button above, or wait for the nightly schedule."
          />
        }
      >
        {(runs) => (
          <div className="overflow-hidden rounded-xl border">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
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
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
