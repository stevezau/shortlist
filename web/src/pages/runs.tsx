import {
  CalendarClock,
  CircleCheck,
  CircleX,
  ListChecks,
  Play,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { RunRowsDialog } from "@/components/runs/run-rows-dialog";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { StatTile } from "@/components/stat-tile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
  formatDuration,
  runElapsedMs,
  runStatusLabel,
  runStatusVariant,
  timeAgo,
  triggerLabel,
} from "@/lib/format";
import {
  useCancelRun,
  useClearRuns,
  useCollections,
  useRuns,
  useRunsSummary,
  useStartRun,
} from "@/lib/queries";
import type { Run, RunsSummary } from "@/lib/types";

function RunsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

/** How long a run took. A finished run shows its fixed duration; a live one ticks up each second. */
export function RunDuration({ run }: { run: Run }) {
  const [now, setNow] = useState(() => Date.now());
  const running = !run.finished_at;
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  if (running) {
    const started = Date.parse(run.started_at);
    const elapsed = Number.isNaN(started) ? null : Math.max(0, now - started);
    return (
      <span className="tabular-nums text-muted-foreground" title="Running…">
        {elapsed != null ? formatDuration(elapsed) : "—"}
      </span>
    );
  }
  const ms = runElapsedMs(run.started_at, run.finished_at);
  return (
    <span className="tabular-nums" title="How long this run took">
      {ms != null ? formatDuration(ms) : "—"}
    </span>
  );
}

function RunRow({ run }: { run: Run }) {
  const cancel = useCancelRun();
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
      <TableCell className="text-muted-foreground">
        <RunDuration run={run} />
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
          {!run.finished_at && (
            <Button
              variant="destructive"
              size="sm"
              className="h-6 px-2 text-xs"
              loading={cancel.isPending}
              disabled={cancel.isPending || cancel.isSuccess}
              onClick={() => cancel.mutate(run.id)}
              title="Stop this run. It finishes the person it's on, then stops."
            >
              {!cancel.isPending && <X aria-hidden="true" />}
              {cancel.isSuccess ? "Stopping…" : "Cancel"}
            </Button>
          )}
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
          <span>
            {run.stats.users_ok} ok
            {/* A skipped person built nothing but nothing went wrong — counting them as "ok" made a
                run where everyone was skipped read as a clean success. */}
            {(run.stats.users_skipped ?? 0) > 0 && (
              <span className="text-warning">
                {" "}
                · {run.stats.users_skipped} skipped
              </span>
            )}
            {run.stats.users_error > 0 && (
              <span className="text-destructive">
                {" "}
                · {run.stats.users_error} failed
              </span>
            )}
          </span>
          {((run.stats.titles_added ?? 0) > 0 ||
            (run.stats.titles_removed ?? 0) > 0) && (
            <span title="Titles added to / rotated out of rows this run">
              ·{" "}
              <span className="text-success">
                +{run.stats.titles_added ?? 0}
              </span>
              /−{run.stats.titles_removed ?? 0}
            </span>
          )}
          {(run.stats.titles_requested ?? 0) > 0 && (
            <span title="Titles requested from Sonarr/Radarr">
              · {run.stats.titles_requested} requested
            </span>
          )}
          {(run.stats.llm_tokens ?? 0) > 0 && (
            <span title="AI tokens this run cost">
              · {run.stats.llm_tokens!.toLocaleString()} tokens
            </span>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}

/** The headline totals above the runs table: how many, how many worked, and when the last one ran. */
function RunsStats({ summary }: { summary: RunsSummary }) {
  return (
    <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile
        icon={ListChecks}
        label="Runs"
        value={summary.total}
        hint="recorded"
      />
      <StatTile
        icon={CircleCheck}
        label="Succeeded"
        value={summary.ok}
        hint="finished cleanly"
        tone="success"
      />
      <StatTile
        icon={CircleX}
        label="Failed"
        value={summary.error}
        hint="ended in error"
        tone={summary.error > 0 ? "destructive" : "default"}
      />
      <StatTile
        icon={CalendarClock}
        label="Last run"
        value={summary.last_finished ? timeAgo(summary.last_finished) : "never"}
        hint={summary.last_status ? runStatusLabel(summary.last_status) : "—"}
      />
    </div>
  );
}

export function RunsPage() {
  // A row links here as /runs?row=<slug> to show only the runs that built it.
  const [params] = useSearchParams();
  const rowSlug = params.get("row") ?? undefined;
  const runsQuery = useRuns(rowSlug);
  const summary = useRunsSummary();
  const collections = useCollections();
  const startRun = useStartRun();
  const clearRuns = useClearRuns();
  const [clearOpen, setClearOpen] = useState(false);
  const rowName =
    rowSlug && collections.data
      ? collections.data.find((c) => c.slug === rowSlug)?.name
      : undefined;

  return (
    <div>
      <PageHeader
        icon={ListChecks}
        title="Runs"
        subtitle="Every time Shortlist rebuilt rows, and how it went."
        actions={
          <div className="flex flex-wrap gap-2">
            {!rowSlug && (summary.data?.total ?? 0) > 0 && (
              <Button
                variant="ghost"
                className="text-muted-foreground"
                onClick={() => setClearOpen(true)}
              >
                <Trash2 aria-hidden="true" />
                Clear runs
              </Button>
            )}
            <RunRowsDialog
              onRun={(collection_ids) => startRun.mutate({ collection_ids })}
              isPending={startRun.isPending}
            />
            <Button
              onClick={() => startRun.mutate({})}
              loading={startRun.isPending}
            >
              {!startRun.isPending && <Play aria-hidden="true" />}
              Run all rows now
            </Button>
          </div>
        }
      />

      {/* Page-level stats, but not while filtered to one row (they'd describe every run, not this row). */}
      {!rowSlug && summary.data && summary.data.total > 0 && (
        <RunsStats summary={summary.data} />
      )}

      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear all run history?</DialogTitle>
            <DialogDescription>
              This deletes every recorded run and the picks they produced. It
              changes nothing on Plex — your rows stay exactly as they are — but
              it also resets the dashboard’s watch tracking, which is built from
              those picks. This can’t be undone.
            </DialogDescription>
          </DialogHeader>
          {clearRuns.isError && (
            <MutationAlert
              error={clearRuns.error}
              fallback="Couldn’t clear the runs. Try again."
            />
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setClearOpen(false)}>
              Keep them
            </Button>
            <Button
              variant="destructive"
              loading={clearRuns.isPending}
              onClick={() =>
                clearRuns.mutate(undefined, {
                  onSuccess: () => setClearOpen(false),
                })
              }
            >
              {!clearRuns.isPending && <Trash2 aria-hidden="true" />}
              Clear run history
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* A refused or failed run says why in plain English (e.g. PMS too old, Plex unreachable).
          Swallowing that left the button looking like it had done nothing at all. */}
      {startRun.isError && (
        <MutationAlert
          className="mb-4"
          error={startRun.error}
          fallback="Couldn’t start that run. Check the server log and try again."
        />
      )}

      {/* Filtered to one row (linked from the Rows page) — say so, and offer a way back to all runs. */}
      {rowSlug && (
        <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">Showing runs that built</span>
          <Badge variant="secondary" className="font-normal">
            {rowName ?? rowSlug}
          </Badge>
          <Button asChild variant="ghost" size="sm">
            <Link to="/runs">
              <X aria-hidden="true" />
              Show all runs
            </Link>
          </Button>
        </div>
      )}

      <QueryBoundary
        query={runsQuery}
        skeleton={<RunsSkeleton />}
        isEmpty={(runs) => runs.length === 0}
        empty={
          <EmptyState
            title={rowSlug ? "No runs for this row yet" : "No runs yet"}
            hint={
              rowSlug
                ? "This row hasn't been built in any recorded run yet. It'll show up here after its next run."
                : "Shortlist hasn't built any rows so far. Start one with the button above, or wait for the nightly schedule."
            }
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
                  <TableHead>Duration</TableHead>
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
