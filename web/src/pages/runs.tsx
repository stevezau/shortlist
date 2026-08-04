import {
  CalendarClock,
  CircleCheck,
  CircleX,
  ListChecks,
  Play,
  Trash2,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

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
  RUNS_PAGE,
  useCancelRun,
  useClearRuns,
  useCollections,
  useRunsPaged,
  useRunsSummary,
  useStartRun,
} from "@/lib/queries";
import type { Run, RunsSummary } from "@/lib/types";
import { useLiveClock } from "@/lib/use-live-clock";

function RunsSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

/** When a run started, as relative time. A live run re-reads the clock on the same second-by-second
 *  tick as its duration — computed once at render, "8m ago" sat frozen next to a duration reading
 *  "10m 54s" until something else re-rendered the row (#67). */
export function RunStarted({ run }: { run: Run }) {
  const now = useLiveClock(!run.finished_at);
  return <>{timeAgo(run.started_at, now)}</>;
}

/** How long a run took. A finished run shows its fixed duration; a live one ticks up each second. */
export function RunDuration({ run }: { run: Run }) {
  const running = !run.finished_at;
  const now = useLiveClock(running);

  if (running) {
    const started = Date.parse(run.started_at ?? "");
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
        <RunStarted run={run} />
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
              <span className="text-destructive-text">
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
  const runsQuery = useRunsPaged(rowSlug);
  // A page is a page of the SAME list; flattening here keeps every consumer below unaware that
  // the history is fetched in chunks.
  const runs = (runsQuery.data?.pages ?? []).flat();
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
              This empties the list below and the step-by-step record of each
              run. Everything the Dashboard counts &mdash; what was put in
              people&rsquo;s rows, and what they went on to watch &mdash; is
              kept. Nothing changes on Plex.
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
        isEmpty={() => runs.length === 0}
        empty={
          <EmptyState
            title={rowSlug ? "No runs for this row yet" : "No runs yet"}
            hint={
              rowSlug
                ? "This row hasn't been built in any recorded run yet. It'll show up here after its next run."
                : // There is no single global schedule any more — each row carries its own cron
                  // (Collection.schedule), and a row with a blank one never runs on a timer at all.
                  "Shortlist hasn't built any rows so far. Start one with the button above, or wait for a row to reach its own schedule — each row is given one in its editor, on the Rows page."
            }
          />
        }
      >
        {() => (
          <div className="space-y-3">
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
            {/* Explicit, not infinite scroll: this is an ops list people read to find one run, and
                a page that grows as you scroll makes "the oldest one" unreachable. */}
            {runsQuery.hasNextPage && (
              <div className="flex justify-center">
                <Button
                  variant="outline"
                  onClick={() => void runsQuery.fetchNextPage()}
                  loading={runsQuery.isFetchingNextPage}
                >
                  Load {RUNS_PAGE} more
                </Button>
              </div>
            )}
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
