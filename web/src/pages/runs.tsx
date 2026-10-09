import { ChevronRight, Clock, ListChecks, Play, Trash2, X } from "lucide-react";
import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { OverflowMenu } from "@/components/rows/overflow-menu";
import { PageHeader } from "@/components/page-header";
import { StatusCell, StatusRow, StatusStrip } from "@/components/status-strip";
import { RunRowsDialog } from "@/components/runs/run-rows-dialog";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
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
  runStatusVariant,
  timeAgo,
  triggerLabel,
} from "@/lib/format";
import {
  RUNS_PAGE,
  queryKeys,
  useCancelRun,
  useClearRuns,
  useCollections,
  useRunsPaged,
  useRunsSummary,
  useSchedule,
  useStartRun,
} from "@/lib/queries";
import { latestFinishedRun, nextRowRun } from "@/lib/dashboard-status";
import { hasPrivacyWarning, privacyFindings } from "@/lib/run-privacy";
import { historyHint, runHealth } from "@/lib/run-status";
import { useSSE } from "@/lib/sse";
import { dayTime } from "@/lib/when";
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
 *  "10m 54s" until something else re-rendered the row (#67).
 *
 *  A queued run says "queued", because `started_at` is stamped at INSERT: for a run still waiting on
 *  the writer lock this column was reporting the moment it was asked for as the moment it began. The
 *  time is kept — it is the useful part — and only the claim about what it means is corrected. */
export function RunStarted({ run }: { run: Run }) {
  const now = useLiveClock(!run.finished_at);
  const when = timeAgo(run.started_at, now);
  return <>{run.status === "queued" ? `queued ${when}` : when}</>;
}

/**
 * How long a run took. A finished run shows its fixed duration; a live one ticks up each second.
 *
 * A QUEUED run shows neither, because it has not started. `runs.started_at` is stamped by the
 * column default at INSERT — the moment the run is queued, not the moment it begins — so treating
 * "no finish time" as "running" made a run that was still waiting for the writer lock tick a
 * duration up from the button press, under a tooltip reading "Running…" beside a badge reading
 * "queued". Same rule as `jobDuration`: a duration requires the work to have started.
 */
export function RunDuration({ run }: { run: Run }) {
  const queued = run.status === "queued";
  const running = !queued && !run.finished_at;
  const now = useLiveClock(running);

  if (queued) {
    return (
      <span
        className="tabular-nums text-muted-foreground"
        title="Waiting to start — nothing is running yet, so there is no duration to show."
      >
        —
      </span>
    );
  }
  if (running) {
    // Tick from when it BEGAN. A run still waiting its turn has not begun, so it counts nothing.
    const started = Date.parse(run.began_at ?? "");
    const elapsed = Number.isNaN(started) ? null : Math.max(0, now - started);
    return (
      <span className="tabular-nums text-muted-foreground" title="Running…">
        {elapsed != null ? formatDuration(elapsed) : "—"}
      </span>
    );
  }
  // A run cancelled while still queued never executed, so it has no duration. It used to be measured
  // from `started_at`, which is set when the row is CREATED — so three runs queued together and
  // cancelled nine minutes later each claimed nine minutes of work none of them had done.
  if (!run.began_at) {
    return (
      <span className="text-muted-foreground" title="This run never started">
        never ran
      </span>
    );
  }
  const ms = runElapsedMs(run.began_at, run.finished_at);
  return (
    <span className="tabular-nums" title="How long this run took">
      {ms != null ? formatDuration(ms) : "—"}
    </span>
  );
}

function RunRow({ run }: { run: Run }) {
  const cancel = useCancelRun();
  const navigate = useNavigate();
  const health = runHealth(run);
  return (
    // The whole row opens the run; the "#N" link stays for keyboard and middle-click.
    <TableRow
      className="group relative grid cursor-pointer grid-cols-2 gap-x-3 px-2 py-2 md:table-row md:p-0"
      onClick={() => void navigate(`/runs/${run.id}`)}
    >
      <TableCell>
        <Link
          to={`/runs/${run.id}`}
          className="rounded-sm font-medium tabular-nums group-hover:text-primary group-hover:underline"
        >
          #{run.id}
        </Link>
      </TableCell>
      <TableCell className="text-right text-muted-foreground md:text-left">
        {triggerLabel(run.trigger)}
      </TableCell>
      <TableCell
        className="text-muted-foreground"
        title={formatDate(run.started_at)}
      >
        <RunStarted run={run} />
      </TableCell>
      <TableCell className="text-muted-foreground">
        <span className="mr-1 text-xs md:hidden">Duration:</span><RunDuration run={run} />
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          <Badge variant={health.tone === "warn" ? "warning" : runStatusVariant(run.status)}>
            {health.label}
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
              onClick={(event) => {
                event.stopPropagation();
                cancel.mutate(run.id);
              }}
              title="Stop this run. It finishes the person it's on, then stops."
            >
              {!cancel.isPending && <X aria-hidden="true" />}
              {cancel.isSuccess ? "Stopping…" : "Cancel"}
            </Button>
          )}
        </div>
      </TableCell>
      <TableCell className="text-muted-foreground">
        {/* Each figure after the first carries its own "·"; the -ml + overflow-hidden pair clips the
            one that lands at the start of a wrapped line, so no line ever opens on a separator. */}
        <div className="overflow-hidden">
        <div className="-ml-4 flex flex-wrap items-center gap-y-0.5 [&>*]:before:inline-block [&>*]:before:w-4 [&>*]:before:text-center [&>*]:before:content-['·']">
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
          {/* The words ARE the legend. "+60/−0" needed one and had none — while the run's own
              page labels the same two figures "added" and "rotated out", so the list and the
              detail described one fact in two vocabularies. */}
          {(run.stats.titles_added ?? 0) > 0 && (
            <span>
              <span className="text-success">+{run.stats.titles_added}</span>{" "}
              added
            </span>
          )}
          {(run.stats.titles_removed ?? 0) > 0 && (
            <span>−{run.stats.titles_removed} rotated out</span>
          )}
          {(run.stats.titles_requested ?? 0) > 0 && (
            <span title="Titles requested from Sonarr/Radarr">
              {run.stats.titles_requested} requested
            </span>
          )}
          {(run.stats.llm_tokens ?? 0) > 0 && (
            <span title="AI input + output tokens this run, as the provider reported them">
              {run.stats.llm_tokens!.toLocaleString()} tokens
            </span>
          )}
        </div>
        </div>
      </TableCell>
      <TableCell className="hidden w-8 text-right md:table-cell">
        <ChevronRight
          aria-hidden="true"
          className="ml-auto h-4 w-4 text-faint-foreground group-hover:text-foreground"
        />
      </TableCell>
    </TableRow>
  );
}

/** The strip above the runs table: the last run, the next one, how many are recorded, and how many warned. */
function RunsStats({ summary, runs }: { summary: RunsSummary; runs: Run[] }) {
  const schedule = useSchedule();
  const last = latestFinishedRun(runs);
  const health = last ? runHealth(last) : null;
  const warned = runs.filter(hasPrivacyWarning);
  const firstWarned = warned[0];
  const next = nextRowRun(schedule.data);
  const failed = summary.error > 0;
  return (
    <StatusStrip label="Run history" className="mb-5">
      <StatusRow className="md:grid-cols-4 md:[&>*:last-child:nth-child(odd)]:col-span-1">
        <StatusCell
          label="Last run"
          tone={health?.tone ?? "neutral"}
          value={
            health ? (
              <Badge variant={health.tone === "warn" ? "warning" : health.tone === "ok" ? "success" : "destructive"}>
                {health.label}
              </Badge>
            ) : (
              "never"
            )
          }
          sub={summary.last_finished ? timeAgo(summary.last_finished) : "Nothing has run yet"}
        />
        <StatusCell
          icon={Clock}
          label="Next run"
          value={next ? dayTime(next.at) : "Not scheduled"}
          sub={next ? timeAgo(next.at) : "No row has a schedule"}
        />
        <StatusCell
          icon={ListChecks}
          label="Runs recorded"
          tone={failed ? "error" : "neutral"}
          value={summary.total}
          sub={historyHint(summary, runs)}
        />
        <StatusCell
          label="With warnings"
          tone={warned.length > 0 ? "warn" : "ok"}
          value={warned.length}
          sub={
            firstWarned
              ? `Run #${firstWarned.id} · ${privacyFindings(firstWarned.privacy).join(", ")}`
              : "None in the runs shown"
          }
        />
      </StatusRow>
    </StatusStrip>
  );
}

export function RunsPage() {
  // A row links here as /runs?row=<slug> to show only the runs that built it.
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const rowSlug = params.get("row") ?? undefined;
  const runsQuery = useRunsPaged(rowSlug);
  // A page is a page of the SAME list; flattening here keeps every consumer below unaware that
  // the history is fetched in chunks.
  const runs = (runsQuery.data?.pages ?? []).flat();
  const summary = useRunsSummary();
  const collections = useCollections();
  const startRun = useStartRun();
  // Live updates. Without this the list is a snapshot: a run that finishes leaves its row reading
  // "Running" with a ticking timer for as long as the page stays open, because nothing refetches. On
  // a real server that made a cancel that HAD worked look like one that was ignored — the operator
  // watches this page, and this page never changed its mind (SFLIX, 2026-08-13).
  useSSE({
    onRunFinished: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.runs });
    },
  });
  const clearRuns = useClearRuns();
  const [clearOpen, setClearOpen] = useState(false);
  const rowName =
    rowSlug && collections.data
      ? collections.data.find((c) => c.slug === rowSlug)?.name
      : undefined;

  return (
    <div>
      <PageHeader
        title="Runs"
        subtitle="Every time Shortlist rebuilt rows, and how it went."
        actions={
          <div className="flex flex-wrap gap-2">
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
            {!rowSlug && (summary.data?.total ?? 0) > 0 && (
              <OverflowMenu
                label="More run actions"
                items={[
                  {
                    label: "Clear run history",
                    icon: Trash2,
                    danger: true,
                    onSelect: () => setClearOpen(true),
                  },
                ]}
              />
            )}
          </div>
        }
      />

      {/* Page-level stats, but not while filtered to one row (they'd describe every run, not this row). */}
      {!rowSlug && summary.data && summary.data.total > 0 && (
        <RunsStats summary={summary.data} runs={runs} />
      )}

      <Dialog open={clearOpen} onOpenChange={setClearOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Clear all run history?</DialogTitle>
            <DialogDescription>
              Permanently deletes the run list and step-by-step logs. Your
              Plex rows, saved picks and watched-pick counts are kept, and
              watch tracking continues.
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
              Keep run history
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
            title={rowSlug ? "No runs for this row yet" : "No run history"}
            hint={
              rowSlug
                ? "This row hasn't been built in any recorded run yet. It'll show up here after its next run."
                : // There is no single global schedule any more — each row carries its own cron
                  // (Collection.schedule), and a row with a blank one never runs on a timer at all.
                  "New runs will appear here. Start one above, or check row schedules on the Rows page."
            }
          />
        }
      >
        {() => (
          <div className="space-y-3">
            <div className="overflow-hidden rounded-xl border">
              <Table>
                <TableHeader className="hidden md:table-header-group">
                  <TableRow className="hover:bg-transparent">
                    {/* Six columns overran a 320px phone by ~55px, and the one pushed outside the
                        card was Users — the column that says how the run actually went. Trigger and
                        Duration are the two a narrow screen can spare: both are on the run's own
                        page, one tap away. */}
                    <TableHead>Run</TableHead>
                    <TableHead className="hidden sm:table-cell">
                      Trigger
                    </TableHead>
                    <TableHead>Started</TableHead>
                    <TableHead className="hidden md:table-cell">
                      Duration
                    </TableHead>
                    <TableHead>Result</TableHead>
                    <TableHead>Users</TableHead>
                    <TableHead className="hidden w-8 md:table-cell">
                      <span className="sr-only">Open</span>
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody className="grid md:table-row-group">
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
