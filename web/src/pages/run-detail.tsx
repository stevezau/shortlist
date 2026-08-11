import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Info, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { BackLink } from "@/components/back-link";
import {
  QueryBoundary,
  EmptyState,
  ErrorState,
} from "@/components/query-boundary";
import { RunLogPanel } from "@/components/runs/run-log-panel";
import { RunPhaseTimeline } from "@/components/runs/run-phase-timeline";
import { RunStatTiles } from "@/components/runs/run-stat-tiles";
import { RunUsersTab } from "@/components/runs/run-users-tab";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import {
  formatDate,
  runStatusLabel,
  runStatusVariant,
  triggerLabel,
} from "@/lib/format";
import { queryKeys, useCancelRun, useRun, useUsers } from "@/lib/queries";
import { mergeRunLog, stageBelongsToRun } from "@/lib/run-log";
import { currentPhase } from "@/lib/run-format";
import { useSSE } from "@/lib/sse";
import type { RunDetail, RunLogEntry, RunUserStageEvent } from "@/lib/types";

/** Why a run failed for a reason that belongs to no single person — a share filter Plex refused, a
 *  sweep that could not run. The reason was always recorded, but lived only in `stats.error`, which
 *  nothing rendered: the page said "Failed" and left the operator reading container logs (issue #1). */
function RunFailureBanner({ run }: { run: RunDetail }) {
  const blockers = run.promotion_blockers ?? [];
  if (run.status !== "error" || (!run.error && blockers.length === 0))
    return null;
  return (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
    >
      <p className="font-medium text-foreground">
        {blockers.length > 0
          ? "Nothing was promoted — Plex wouldn’t accept a share filter"
          : "This run didn’t finish cleanly"}
      </p>
      {blockers.length > 0 && (
        <p className="text-muted-foreground">
          Rows are only put on Home once every other account is set to hide
          them. Plex refused that change for{" "}
          {blockers.length === 1
            ? "this account"
            : `${blockers.length} accounts`}
          , so the rows were built but deliberately left hidden rather than risk
          showing one person’s row to someone else.
        </p>
      )}
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2.5 font-mono text-xs text-destructive-text">
        {blockers.length > 0 ? blockers.join("\n") : run.error}
      </pre>
    </div>
  );
}

/** The two things that differ per tab. The metrics and the failure banner are NOT tabbed: they are
 *  the answer to "how did this run go", which you want regardless of which detail you came for. */
type RunTab = "users" | "log";

export function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const runQuery = useRun(runId, Number.isFinite(runId));
  const usersQuery = useUsers();
  const queryClient = useQueryClient();
  const cancel = useCancelRun();
  // From the RUN, not just this component's mutation state: a refresh threw that away, so the
  // button came back looking live on a run that was already stopping — and every press after that
  // returned "this run isn't currently running" about a run that was.
  const stopping =
    cancel.isSuccess || runQuery.data?.stats?.cancel_requested === true;
  // Tab and the deep-linked person both live in the URL, so a refresh, a bookmark, and the link
  // from a person's Runs tab all land exactly where they said they would.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get("tab") as RunTab | null) ?? "users";
  const linkedUser = searchParams.get("user") ?? "";
  const setTab = (next: RunTab) => {
    const params = new URLSearchParams(searchParams);
    if (next === "users") params.delete("tab");
    else params.set("tab", next);
    setSearchParams(params, { replace: true });
  };

  // Run results carry slug/username but no user id, so map slug → id to deep-link each result to
  // its user page. Users removed from Plex since the run won't be in the map — those stay plain text.
  const idBySlug = new Map(
    (usersQuery.data ?? []).map((user) => [user.slug, user.id]),
  );

  // The activity log: seed from the server's in-memory buffer, then top it up live from the SSE
  // stage stream. Held in a ref+state so appends don't depend on stale closures.
  const logQuery = useQuery({
    queryKey: queryKeys.runLog(runId),
    queryFn: () => api.getRunLog(runId),
    enabled: Number.isFinite(runId),
  });
  const [liveLog, setLiveLog] = useState<RunLogEntry[]>([]);
  // Seed from the server snapshot; mergeRunLog dedups, so re-merging the same data is a no-op and an
  // event captured by BOTH the snapshot and the live stream is never doubled.
  // Effect is the right tool here and the rule is suppressed deliberately: this state is a merge of
  // two external sources (the fetched snapshot and the live SSE stream), so it cannot be derived
  // from props — there is nothing to derive it FROM until the query resolves.
  /* eslint-disable react-hooks/set-state-in-effect */
  useEffect(() => {
    if (logQuery.data) {
      setLiveLog((prev) => mergeRunLog(prev, logQuery.data, runId));
    }
  }, [logQuery.data, runId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const appendStage = useCallback(
    (event: RunUserStageEvent) => {
      setLiveLog((prev) => mergeRunLog(prev, [event], runId));
    },
    [runId],
  );

  // Keep an in-flight run's page live: refetch on every stage/finish event, and append the stage to
  // the activity log so it scrolls in real time. Guarded on run_id — appendStage/mergeRunLog already
  // drop events for another run from the LOG, but the refetch used to fire regardless, so sitting on
  // finished run #12 while run #40 streamed refetched #12 on every one of #40's events.
  useSSE({
    onRunUserStage: (event) => {
      appendStage(event);
      if (stageBelongsToRun(event, runId)) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
      }
    },
    onRunFinished: (event) => {
      if (event.run_id === runId) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      }
    },
  });

  // Which user's rows are on screen. Default to the first FAILED user (what you opened the page to
  // see), else the first user; keep the current pick as long as they're still in the run.
  // Derived, not synced by an effect: hold only what the user explicitly clicked, and fall back
  // whenever that person isn't in the run (first load, or a refetch that dropped them). The effect
  // version had to list selectedSlug in its own deps to re-check itself, which is the shape that
  // makes cascading renders easy to introduce.
  const [pickedSlug, setSelectedSlug] = useState("");
  const runUsers = runQuery.data?.users ?? [];
  // `?user=` wins on first load — it is how a person's own Runs tab links here — but only until
  // something else is clicked, which is what `pickedSlug` records.
  const requested = pickedSlug || linkedUser;
  const selectedSlug = runUsers.some((u) => u.slug === requested)
    ? requested
    : ((runUsers.find((u) => u.error !== null) ?? runUsers[0])?.slug ?? "");

  // Computed once per render rather than called twice (header line + phase text below it).
  const phase = currentPhase(liveLog);
  // A failed log fetch with nothing to show is otherwise indistinguishable from "no log was ever
  // recorded" — RunLogPanel's own empty state says the latter, which is a lie when the former is
  // true. Live SSE stage events can still fill `liveLog` even if the initial snapshot failed, so
  // this only takes over when there is truly nothing to show.
  const logFailed = logQuery.isError && liveLog.length === 0;

  return (
    <div className="space-y-6">
      <BackLink to="/runs" label="All runs" />

      {!Number.isFinite(runId) ? (
        <EmptyState
          title="That run doesn’t exist"
          hint="The link may be wrong or the run was removed."
          action={
            <Button asChild variant="outline">
              <Link to="/runs">Back to all runs</Link>
            </Button>
          }
        />
      ) : (
        <QueryBoundary
          query={runQuery}
          skeleton={<Skeleton className="h-64 w-full" />}
        >
          {(run) => (
            <div className="space-y-6">
              <header className="space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h1 className="text-2xl font-semibold tracking-tight">
                    Run #{run.id}
                  </h1>
                  <Badge variant={runStatusVariant(run.status)}>
                    {runStatusLabel(run.status)}
                  </Badge>
                  {run.dry_run && (
                    <Badge variant="outline">
                      Test run — nothing was written to Plex
                    </Badge>
                  )}
                  {!run.finished_at && (
                    <Button
                      variant="destructive"
                      size="sm"
                      className="ml-auto"
                      loading={cancel.isPending}
                      disabled={cancel.isPending || stopping}
                      onClick={() => cancel.mutate(run.id)}
                      title="Stop this run. It finishes the person it's on, then stops — everyone already done stays."
                    >
                      {stopping ? "Stopping…" : "Cancel run"}
                    </Button>
                  )}
                </div>
                {/* A slim provenance line; the numbers moved into the tiles below so they read at a glance. */}
                <p className="text-sm text-muted-foreground">
                  {triggerLabel(run.trigger)} · started{" "}
                  {formatDate(run.started_at)}
                  {run.finished_at
                    ? ` · finished ${formatDate(run.finished_at)}`
                    : " · still running"}
                </p>
                {/* The direct fix for "all users finished but it still says running": say WHAT it
                    is doing. Everything after the last person is server-wide and used to be silent. */}
                {!run.finished_at && phase && (
                  <p className="flex items-center gap-1.5 text-sm">
                    <Loader2
                      className="h-3.5 w-3.5 animate-spin text-muted-foreground"
                      aria-hidden="true"
                    />
                    <span className="text-muted-foreground">
                      Finishing up ·{" "}
                    </span>
                    <span className="font-medium">{phase}</span>
                  </p>
                )}
              </header>

              <Segmented
                value={tab}
                onChange={setTab}
                ariaLabel="Run detail sections"
                options={[
                  { value: "users", label: `People (${run.users.length})` },
                  {
                    value: "log",
                    label: liveLog.length ? `Log (${liveLog.length})` : "Log",
                  },
                ]}
              />

              <RunFailureBanner run={run} />

              {/* Stats are only finalized once a run ends; while it's live we show the why-slow note instead. */}
              {/* The TILES stay on both tabs — they are the run's summary, not one view of it. The
                  phase breakdown does not: "where the time went" is read off the log's own timings and
                  answers a question you are asking while reading the log, not while scanning people. */}
              {run.finished_at && <RunStatTiles run={run} />}

              {/* No log peek here. It duplicated the Log tab sitting one click away, and on a live
                  run it churned under the header while you were trying to read the people list —
                  the Log tab is the place to watch a run, not this one. */}

              {!run.finished_at && (
                <div className="flex gap-3 rounded-lg border bg-muted/40 p-4 text-sm">
                  <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                  <div className="space-y-1">
                    <p className="font-medium">
                      Why a refresh can take a while
                    </p>
                    <p className="text-muted-foreground">
                      Building everyone's rows means updating Plex one change at
                      a time — Plex only accepts them one-by-one, and it's
                      especially slow to update TV Shows. A big refresh across
                      all users can take a while. It's much quicker after the
                      first run, when most rows only need small tweaks.
                    </p>
                  </div>
                </div>
              )}

              {tab === "users" &&
                (run.users.length === 0 ? (
                  <EmptyState
                    title={
                      run.finished_at
                        ? "No per-user results"
                        : "Working — results appear as each user finishes"
                    }
                    hint={
                      run.finished_at
                        ? "This run didn't process any users."
                        : "Each person's picks land here when they finish; the Log tab shows live progress."
                    }
                  />
                ) : (
                  <RunUsersTab
                    run={run}
                    selectedSlug={selectedSlug}
                    onSelect={setSelectedSlug}
                    idBySlug={idBySlug}
                    liveLog={liveLog}
                  />
                ))}

              {tab === "log" &&
                (logFailed ? (
                  <ErrorState
                    error={logQuery.error}
                    onRetry={() => void logQuery.refetch()}
                  />
                ) : (
                  <>
                    {run.finished_at && <RunPhaseTimeline entries={liveLog} />}
                    <RunLogPanel
                      runId={run.id}
                      entries={liveLog}
                      running={!run.finished_at}
                      people={[...new Set(run.users.map((u) => u.slug))].sort()}
                    />
                  </>
                ))}
            </div>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
