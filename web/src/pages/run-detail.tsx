import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Play } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import {
  QueryBoundary,
  EmptyState,
  ErrorState,
} from "@/components/query-boundary";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { RunLogPanel } from "@/components/runs/run-log-panel";
import { RunPhaseTimeline } from "@/components/runs/run-phase-timeline";
import { RunPrivacyCallout } from "@/components/runs/run-privacy-callout";
import { RunStatTiles } from "@/components/runs/run-stat-tiles";
import { RunRowsTab } from "@/components/runs/run-rows-tab";
import { Tabs, TabPanel } from "@/components/ui/tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, apiUrl } from "@/lib/api";
import {
  formatDate,
  runStatusLabel,
  runStatusVariant,
  timeAgo,
  triggerLabel,
} from "@/lib/format";
import {
  queryKeys,
  useCancelRun,
  useCollections,
  useRun,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import { mergeRunLog, stageBelongsToRun } from "@/lib/run-log";
import { errorBucket } from "@/lib/run-format";
import { privacyFindings } from "@/lib/run-privacy";
import { runHealth } from "@/lib/run-status";
import { RunProgress } from "@/components/runs/run-progress";
import { useHashScroll } from "@/lib/use-hash-scroll";
import { useSSE } from "@/lib/sse";
import type { RunDetail, RunLogEntry, RunUserStageEvent } from "@/lib/types";
import { personName } from "@/lib/user-names";

/** The people a run failed for, grouped by the reason — because 45 people can share one cause.
 *
 * Grouped on `errorBucket`, NOT on the raw string. The engine stores
 * `f"{type(e).__name__}: {e}"` (`pipeline.py:361`), and a plexapi message embeds the per-user
 * ratingKey and row title — so two people felled by one PMS outage never produce equal strings, and
 * grouping on them reported "4 people didn't get their rows, for 4 different reasons" about a
 * single 500. That is the exact opposite of what this banner exists to tell you.
 *
 * `errorBucket` returns null for anything it does not recognise, and those keep their own line:
 * claiming two unrecognised errors are "the same problem" would be the same lie in the other
 * direction.
 */
function failuresByReason(
  run: RunDetail,
): { reason: string; people: string[] }[] {
  const groups = new Map<string, { reason: string; people: string[] }>();
  for (const user of run.users ?? []) {
    if (!user.error) continue;
    const bucket = errorBucket(user.error);
    // Unrecognised errors group by their own text, so they are never merged with each other.
    const key = bucket ?? `raw:${user.error}`;
    const group = groups.get(key) ?? { reason: user.error, people: [] };
    group.people.push(personName(user));
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => b.people.length - a.people.length);
}

/** Says how many people, and whether it was one cause or several — the two facts that decide
 *  whether this is "one bad account" or "the server was down". */
function peopleFailedHeadline(
  groups: { reason: string; people: string[] }[],
): string {
  const people = groups.reduce((n, g) => n + g.people.length, 0);
  const who = people === 1 ? "1 person" : `${people} people`;
  return groups.length > 1
    ? `${who} didn’t get their rows, for ${groups.length} different reasons`
    : `${who} didn’t get their rows`;
}

/** Why a run failed for a reason that belongs to no single person — a share filter Plex refused, a
 *  sweep that could not run. The reason was always recorded, but lived only in `stats.error`, which
 *  nothing rendered: the page said "Failed" and left the operator reading container logs (issue #1). */
function RunFailureBanner({ run }: { run: RunDetail }) {
  const blockers = run.promotion_blockers ?? [];
  // A run can be `error` with NOTHING at run level: the failure belonged to individual people.
  // Measured on a real server — run 4, `users_ok: 45, users_error: 1`, `stats.error` null and no
  // blockers — so this returned null and the page announced a failed run and then explained
  // nothing, leaving one bad account to be found by eye among forty-six.
  const perUser = failuresByReason(run);
  if (
    run.status !== "error" ||
    (!run.error && blockers.length === 0 && perUser.length === 0)
  )
    return null;
  return (
    <div
      role="alert"
      data-testid="run-failure"
      className="space-y-2 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
    >
      <p className="font-medium text-foreground">
        {blockers.length > 0
          ? "Nothing was promoted — Plex wouldn’t accept a share filter"
          : run.error
            ? "This run didn’t finish cleanly"
            : peopleFailedHeadline(perUser)}
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
      {blockers.length === 0 && !run.error ? (
        // Grouped by reason and NAMING the people, because the whole difficulty was finding them:
        // one failure among forty-five successes is invisible in a list of forty-six.
        perUser.map((group) => (
          <div key={group.reason} className="space-y-1">
            <p className="text-muted-foreground">
              {group.people.length === 1
                ? group.people[0]
                : `${group.people.length} people: ${group.people.join(", ")}`}
            </p>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2.5 font-mono text-xs text-destructive-text">
              {group.reason}
            </pre>
          </div>
        ))
      ) : (
        // `break-words`, not `break-all`: this is usually a sentence now (an unreachable Plex is
        // explained in words), and `break-all` splits those mid-word. A long URL still wraps.
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-background/60 p-2.5 font-mono text-xs text-destructive-text">
          {blockers.length > 0 ? blockers.join("\n") : run.error}
        </pre>
      )}
    </div>
  );
}

/** The things that differ per tab. The metrics and the failure banner are NOT tabbed: they are
 *  the answer to "how did this run go", which you want regardless of which detail you came for.
 *
 *  `rows` is the default and the primary axis, because a ROW is what a run builds. People-first left
 *  a SHARED row — which belongs to nobody — with nowhere to appear at all, so a run whose only work
 *  was a shared row rendered as a wall of "skipped" with its actual output off screen. There is no
 *  People tab any more: its person list and per-person panel were the right shape and are kept, but
 *  inside the row they belong to rather than as a second way of slicing the same run. */
type RunTab = "rows" | "log";

export function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const runQuery = useRun(runId, Number.isFinite(runId));
  const usersQuery = useUsers();
  const queryClient = useQueryClient();
  const cancel = useCancelRun();
  const startRun = useStartRun();
  const navigate = useNavigate();
  // From the RUN, not just this component's mutation state: a refresh threw that away, so the
  // button came back looking live on a run that was already stopping — and every press after that
  // returned "this run isn't currently running" about a run that was.
  const stopping =
    cancel.isSuccess || runQuery.data?.stats?.cancel_requested === true;
  // Tab and the deep-linked person both live in the URL, so a refresh, a bookmark, and the link
  // from a person's Runs tab all land exactly where they said they would.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: RunTab = searchParams.get("tab") === "log" ? "log" : "rows";
  // Deep link from a person's Recent runs. It survived the People tab's removal as a dead parameter:
  // the link was still built, nothing read it, and clicking "Run #NN" from someone's page landed on
  // the top of a run with forty others in it.
  const focusUser = searchParams.get("user");
  useHashScroll(runQuery.isSuccess);
  const setTab = (next: RunTab) => {
    const params = new URLSearchParams(searchParams);
    if (next === "rows") params.delete("tab");
    else params.set("tab", next);
    setSearchParams(params, { replace: true });
  };

  // Run results carry slug/username but no user id, so map slug → id to deep-link each result to
  // its user page. Users removed from Plex since the run won't be in the map — those stay plain text.
  const idBySlug = new Map(
    (usersQuery.data ?? []).map((user) => [user.slug, user.id]),
  );
  // Row slug → current name, for rows this run delivered no title for. A run where every person was
  // skipped has no `row_title` anywhere in its results, so without this the tree would show slugs.
  const collectionsQuery = useCollections();
  const rowTitles = Object.fromEntries(
    (collectionsQuery.data ?? []).map((row) => [row.slug, row.name]),
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
        // The run's logged warnings and errors reach the log through this read only — the stage
        // stream above carries narration. `mergeRunLog` dedupes by seq, so re-reading is safe.
        void queryClient.invalidateQueries({ queryKey: queryKeys.runLog(runId) });
      }
    },
  });

  // A failed log fetch with nothing to show is otherwise indistinguishable from "no log was ever
  // recorded" — RunLogPanel's own empty state says the latter, which is a lie when the former is
  // true. Live SSE stage events can still fill `liveLog` even if the initial snapshot failed, so
  // this only takes over when there is truly nothing to show.
  const logFailed = logQuery.isError && liveLog.length === 0;
  // Who this run found could see rows that are not theirs, by lower-cased username — the people
  // list marks them "not private". Empty for a run that did not measure: unmeasured is not flagged.
  const notPrivate = new Set(
    privacyFindings(runQuery.data?.privacy).map((name) => name.toLowerCase()),
  );

  return (
    <div className="space-y-6">
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
              <PageHeader
                className="mb-0"
                title={
                  // The breadcrumb is part of the title line, so this page's title sits at the same
                  // height as every other page's.
                  <>
                    <Link to="/runs" className="font-normal text-muted-foreground hover:text-foreground">
                      Runs
                    </Link>
                    <span className="font-normal text-faint-foreground">{" / "}</span>
                    Run #{run.id}
                    {run.finished_at && (
                      <Badge
                        variant={runHealth(run).tone === "warn" ? "warning" : runStatusVariant(run.status)}
                        className="ml-3 align-middle"
                      >
                        {runHealth(run).label}
                      </Badge>
                    )}
                  </>
                }
                // `runs.started_at` is stamped at INSERT — when the run was ASKED for, not when it
                // began — so a run still waiting on the writer lock read "started 03:30 · still
                // running" directly under a badge saying "Queued". This is also the page the Rows
                // page's Run button lands on, which made it the first thing you saw after pressing
                // it. Same rule as RunDuration: don't claim it started until it has.
                subtitle={
                  run.status === "queued" ? (
                    <>
                      {triggerLabel(run.trigger)} · queued{" "}
                      {formatDate(run.started_at)} · waiting to start
                    </>
                  ) : !run.began_at ? (
                    // Cancelled or reaped while still queued. Its status is no longer "queued", so
                    // this used to fall through and claim "started 03:30 · finished 03:39" — the same
                    // nine minutes the list row now correctly calls "never ran", one click away and
                    // directly above a Duration cell reading "—".
                    <>
                      {triggerLabel(run.trigger)} · queued{" "}
                      {formatDate(run.started_at)} · never started
                    </>
                  ) : (
                    // Absolute AND relative: "finished 3 Oct, 02:39" answers which night, "2m ago"
                    // answers whether this is the run you just started.
                    <>
                      {triggerLabel(run.trigger)} · started{" "}
                      {formatDate(run.began_at)}
                      {run.finished_at
                        ? ` · finished ${formatDate(run.finished_at)} · ${timeAgo(run.finished_at)}`
                        : ` · ${timeAgo(run.began_at)} · still running`}
                    </>
                  )
                }
                actions={
                  <>
                    {/* The finished run's result lives in the summary's Result cell. While it is still
                        going there is no summary yet, so the status rides here. */}
                    {run.dry_run && (
                      <Badge variant="outline">
                        Test run — nothing was written to Plex
                      </Badge>
                    )}
                    {/* The same file the Log tab's Download gives, without opening the tab first. */}
                    <Button asChild variant="outline">
                      <a href={apiUrl(`/api/runs/${run.id}/log?format=text`)} download>
                        <Download aria-hidden="true" />
                        Download log
                      </a>
                    </Button>
                    {!run.finished_at && (
                        <>
                          <Badge variant={run.status === "running" ? "warning" : runStatusVariant(run.status)}>
                            {runStatusLabel(run.status)}
                          </Badge>
                          <Button
                            variant="outline"
                            size="sm"
                            loading={cancel.isPending}
                            disabled={cancel.isPending || stopping}
                            onClick={() => cancel.mutate(run.id)}
                            title="Stop this run. It finishes the person it's on, then stops — everyone already done stays."
                          >
                            {stopping ? "Stopping…" : "Cancel run"}
                          </Button>
                        </>
                      )}
                    {/* Outline, not amber: a past run is a record, and re-running it is the same mutation
                        the Runs page uses, then straight to the new run, as the Rows page does. */}
                    {run.finished_at && (
                      <Button
                        variant="outline"
                        loading={startRun.isPending}
                        onClick={() =>
                          startRun.mutate({}, { onSuccess: (created) => void navigate(`/runs/${created.run_id}`) })
                        }
                      >
                        {!startRun.isPending && <Play aria-hidden="true" />}
                        Run again
                      </Button>
                    )}
                  </>
                }
              />

              {/* A refused start says why in plain English (PMS too old, Plex unreachable). */}
              {startRun.isError && (
                <MutationAlert
                  error={startRun.error}
                  fallback="Couldn’t start that run. Check the server log and try again."
                />
              )}

              <RunProgress run={run} entries={liveLog} />

              <RunFailureBanner run={run} />

              {/* The summary is the answer to "how did this run go", so it sits above the tabs and
                  stays on both: the rows and the log are two views of the detail, not of the result.
                  Stats are only final once a run ends; while it is live the progress above says why
                  it is taking the time it is. */}
              {run.finished_at && <RunStatTiles run={run} />}
              {run.finished_at && <RunPrivacyCallout run={run} />}

              <Tabs id="run-detail"
                value={tab}
                onChange={setTab}
                ariaLabel="Run detail sections"
                options={[
                  { value: "rows", label: "Rows" },
                  {
                    value: "log",
                    label: liveLog.length ? `Log (${liveLog.length})` : "Log",
                  },
                ]}
              />

              {/* No log peek here. It duplicated the Log tab sitting one click away, and on a live
                  run it churned under the header while you were trying to read the people list —
                  the Log tab is the place to watch a run, not this one. The phase breakdown lives on
                  the Log tab too: "where the time went" is read off the log's own timings. */}

              {tab === "rows" && (
                <TabPanel id="run-detail" value="rows"><div id="run-rows" className="scroll-mt-20"><RunRowsTab
                  run={run}
                  titles={rowTitles}
                  idBySlug={idBySlug}
                  liveLog={liveLog}
                  focusUser={focusUser}
                  notPrivate={notPrivate}
                /></div></TabPanel>
              )}

              {tab === "log" &&
                <TabPanel id="run-detail" value="log">{logFailed ? (
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
                )}</TabPanel>}
            </div>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
