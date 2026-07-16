import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, Copy, Loader2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { PickList } from "@/components/pick-list";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  formatDate,
  formatDuration,
  runStatusLabel,
  runStatusVariant,
  triggerLabel,
} from "@/lib/format";
import { githubIssueSnippet } from "@/lib/github";
import { queryKeys, useRun, useUsers } from "@/lib/queries";
import { mergeRunLog } from "@/lib/run-log";
import { STAGE_LABELS } from "@/lib/run-stages";
import { useSSE } from "@/lib/sse";
import type {
  Pick,
  RunDetail,
  RunLibraryBreakdown,
  RunLogEntry,
  RunUserResult,
  RunUserStageEvent,
} from "@/lib/types";

/** A run's live activity log: seeded from the server buffer, topped up by the SSE stage stream. */
function ActivityLog({
  entries,
  running,
}: {
  entries: RunLogEntry[];
  running: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  // Follow the tail as new lines arrive, but don't yank the page for reduced-motion users.
  useEffect(() => {
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    )?.matches;
    endRef.current?.scrollIntoView?.({
      block: "nearest",
      behavior: reduce ? "auto" : "smooth",
    });
  }, [entries.length]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-base">Activity</CardTitle>
        {running && (
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            live
          </span>
        )}
      </CardHeader>
      <CardContent>
        <div
          className="max-h-72 space-y-1 overflow-y-auto rounded-md bg-muted/40 p-3 font-mono text-xs"
          role="log"
          aria-live="polite"
          aria-label="Run activity log"
        >
          {entries.length === 0 ? (
            <p className="text-muted-foreground">
              {running ? "Starting…" : "No activity recorded for this run."}
            </p>
          ) : (
            entries.map((entry, i) => <LogLine key={i} entry={entry} />)
          )}
          <div ref={endRef} />
        </div>
      </CardContent>
    </Card>
  );
}

function LogLine({ entry }: { entry: RunLogEntry }) {
  const time = entry.ts ? new Date(entry.ts).toLocaleTimeString() : "";
  const label = STAGE_LABELS[entry.stage] ?? entry.stage;
  const detail = Object.entries(entry.counts ?? {})
    .map(([k, v]) => `${v} ${k}`)
    .join(", ");
  return (
    <div className="flex gap-2">
      {time && <span className="shrink-0 text-muted-foreground">{time}</span>}
      <span className="shrink-0 font-medium">{entry.user}</span>
      <span className="text-muted-foreground">
        {label}
        {detail ? ` · ${detail}` : ""}
      </span>
    </div>
  );
}

function CopyForGitHubButton({
  run,
  result,
}: {
  run: RunDetail;
  result: RunUserResult;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(githubIssueSnippet(run, result));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be unavailable (http, permissions); the button simply
      // stays in its default state — the error text itself is on screen.
    }
  };

  return (
    <Button variant="outline" size="sm" onClick={() => void copy()}>
      {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      {copied ? "Copied" : "Copy for GitHub issue"}
    </Button>
  );
}

/** Rank badge colour by tier — the top picks stand out, lower ones recede. */
function rankClass(rank: number): string {
  if (rank <= 3) return "text-amber-400";
  if (rank <= 10) return "text-foreground";
  return "text-muted-foreground";
}

/** One ranked pick: rank, a status dot (green = new this run), title, and its reason (one line). */
function PickLine({ pick, isNew }: { pick: Pick; isNew: boolean }) {
  return (
    <li className="flex items-baseline gap-3 py-1.5">
      <span
        className={cn(
          "w-9 shrink-0 text-right text-sm font-semibold tabular-nums",
          rankClass(pick.rank),
        )}
      >
        #{pick.rank}
      </span>
      <span
        className={cn(
          "mt-1.5 h-2 w-2 shrink-0 rounded-full",
          isNew ? "bg-success" : "bg-muted-foreground/30",
        )}
        aria-label={isNew ? "new this run" : "kept"}
        title={isNew ? "New this run" : "Kept from last run"}
      />
      <span className="min-w-0 flex-1 truncate text-sm">
        <span className="font-medium">{pick.title}</span>
        {pick.reason && (
          <span className="text-muted-foreground"> — {pick.reason}</span>
        )}
      </span>
    </li>
  );
}

/** One library's ranked picks: first five, a show-all toggle, and a quiet "removed" footer. */
function LibraryPicks({ entry }: { entry: RunLibraryBreakdown }) {
  const [expanded, setExpanded] = useState(false);
  const added = new Set(entry.added);
  const shown = expanded ? entry.picks : entry.picks.slice(0, 5);
  return (
    <div className="space-y-2">
      {entry.picks.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No picks in this library.
        </p>
      ) : (
        <ul className="divide-y divide-border/40">
          {shown.map((pick) => (
            <PickLine
              key={pick.rank}
              pick={pick}
              isNew={added.has(pick.title)}
            />
          ))}
        </ul>
      )}
      {entry.picks.length > 5 && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show fewer" : `Show all ${entry.picks.length}`}
        </Button>
      )}
      {entry.removed.length > 0 && (
        <p className="pt-1 text-xs text-muted-foreground">
          <span className="font-medium text-destructive/80">
            −{entry.removed.length} removed:
          </span>{" "}
          <span className="line-through">{entry.removed.join(", ")}</span>
        </p>
      )}
      {entry.deleted.length > 0 && (
        <p className="text-xs font-medium text-destructive">
          Row deleted: {entry.deleted.join(", ")}
        </p>
      )}
    </div>
  );
}

/** One row (its libraries as tabs when there's more than one), showing the selected library's picks. */
function RowSection({ entries }: { entries: RunLibraryBreakdown[] }) {
  const [libKey, setLibKey] = useState(entries[0]?.library_key ?? "");
  const active =
    entries.find((entry) => entry.library_key === libKey) ?? entries[0];
  const added = entries.reduce((n, entry) => n + entry.added.length, 0);
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{entries[0]?.row_title}</h3>
        {added > 0 && (
          <span className="text-xs text-success">+{added} new</span>
        )}
      </div>
      {entries.length > 1 && (
        <Segmented
          value={libKey}
          onChange={setLibKey}
          ariaLabel="Library"
          options={entries.map((entry) => ({
            value: entry.library_key,
            label: `${entry.library_title} · ${entry.picks.length}`,
          }))}
        />
      )}
      {active && <LibraryPicks entry={active} />}
    </div>
  );
}

/** The selected user's result: an error, or their rows grouped from the per-(row, library) breakdown. */
function UserPanel({ run, result }: { run: RunDetail; result: RunUserResult }) {
  if (result.error !== null) {
    return (
      <div role="alert" className="space-y-3 rounded-md bg-destructive/10 p-3">
        <p className="font-mono text-sm text-destructive">{result.error}</p>
        <CopyForGitHubButton run={run} result={result} />
      </div>
    );
  }
  if (result.breakdown.length === 0) {
    // Still running (this user hasn't finished) or a legacy run with no breakdown.
    if (result.picks.length > 0)
      return <PickList picks={result.picks} className="mt-1" />;
    return (
      <p className="text-sm text-muted-foreground">
        {result.status === "ok" || result.status === "cold_start"
          ? "No changes — this person’s rows were already up to date."
          : "Working on this person…"}
      </p>
    );
  }
  const rows = new Map<string, RunLibraryBreakdown[]>();
  for (const entry of result.breakdown) {
    rows.set(entry.row_slug, [...(rows.get(entry.row_slug) ?? []), entry]);
  }
  return (
    <div className="space-y-6">
      {[...rows.values()].map((entries) => (
        <RowSection key={entries[0]?.row_slug} entries={entries} />
      ))}
    </div>
  );
}

/** The clickable user nav at the top of a run — pick whose rows to see (failures flagged). */
function UserTabs({
  results,
  selected,
  onSelect,
}: {
  results: RunUserResult[];
  selected: string;
  onSelect: (slug: string) => void;
}) {
  return (
    <div
      className="flex flex-wrap gap-2"
      role="tablist"
      aria-label="Users in this run"
    >
      {results.map((result) => {
        const failed = result.error !== null;
        const isSelected = result.slug === selected;
        return (
          <button
            key={result.slug}
            type="button"
            role="tab"
            aria-selected={isSelected}
            onClick={() => onSelect(result.slug)}
            className={cn(
              "flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              isSelected
                ? "border-primary bg-primary/10"
                : "border-border hover:bg-muted",
              failed && !isSelected && "border-destructive/40",
            )}
          >
            <UserAvatar name={result.username} size="sm" />
            <span className="font-medium">{result.username}</span>
            {failed ? (
              <AlertCircle
                className="h-3.5 w-3.5 text-destructive"
                aria-hidden="true"
              />
            ) : (
              <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
            )}
          </button>
        );
      })}
    </div>
  );
}

export function RunDetailPage() {
  const { id } = useParams();
  const runId = Number(id);
  const runQuery = useRun(runId, Number.isFinite(runId));
  const usersQuery = useUsers();
  const queryClient = useQueryClient();

  // Run results carry slug/username but no user id, so map slug → id to deep-link each result to
  // its user page. Users removed from Plex since the run won't be in the map — those stay plain text.
  const idBySlug = new Map(
    (usersQuery.data ?? []).map((user) => [user.slug, user.id]),
  );

  // The activity log: seed from the server's in-memory buffer, then top it up live from the SSE
  // stage stream. Held in a ref+state so appends don't depend on stale closures.
  const logQuery = useQuery({
    queryKey: ["run-log", runId],
    queryFn: () => api.getRunLog(runId),
    enabled: Number.isFinite(runId),
  });
  const [liveLog, setLiveLog] = useState<RunLogEntry[]>([]);
  // Seed from the server snapshot; mergeRunLog dedups, so re-merging the same data is a no-op and an
  // event captured by BOTH the snapshot and the live stream is never doubled.
  useEffect(() => {
    if (logQuery.data) {
      setLiveLog((prev) => mergeRunLog(prev, logQuery.data, runId));
    }
  }, [logQuery.data, runId]);

  const appendStage = useCallback(
    (event: RunUserStageEvent) => {
      setLiveLog((prev) => mergeRunLog(prev, [event], runId));
    },
    [runId],
  );

  // Keep an in-flight run's page live: refetch on every stage/finish event, and append the stage to
  // the activity log so it scrolls in real time.
  useSSE({
    onRunUserStage: (event) => {
      appendStage(event);
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
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
  const [selectedSlug, setSelectedSlug] = useState("");
  useEffect(() => {
    const users = runQuery.data?.users ?? [];
    const first = users[0];
    if (first && !users.some((u) => u.slug === selectedSlug)) {
      setSelectedSlug((users.find((u) => u.error !== null) ?? first).slug);
    }
  }, [runQuery.data, selectedSlug]);

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
                </div>
                <p className="text-sm text-muted-foreground">
                  {triggerLabel(run.trigger)} · started{" "}
                  {formatDate(run.started_at)}
                  {run.finished_at
                    ? ` · finished ${formatDate(run.finished_at)}`
                    : " · still running"}{" "}
                  · {run.stats.users_ok} ok, {run.stats.users_error} failed
                  {run.stats.titles_requested
                    ? ` · ${run.stats.titles_requested} title${
                        run.stats.titles_requested === 1 ? "" : "s"
                      } requested`
                    : ""}
                </p>
              </header>

              {run.users.length === 0 ? (
                <EmptyState
                  title={
                    run.finished_at
                      ? "No per-user results"
                      : "Working — results appear as each user finishes"
                  }
                  hint={
                    run.finished_at
                      ? "This run didn't process any users."
                      : "Each user's picks land here when they finish; the activity log below shows live progress."
                  }
                />
              ) : (
                (() => {
                  // Failures first in the nav, so a partly-failed run opens on the error you came for.
                  const ordered = [...run.users].sort(
                    (a, b) =>
                      Number(b.error !== null) - Number(a.error !== null),
                  );
                  const selected =
                    run.users.find((u) => u.slug === selectedSlug) ??
                    ordered[0];
                  if (!selected) return null;
                  const failed = selected.error !== null;
                  const userId = idBySlug.get(selected.slug) ?? null;
                  return (
                    <div className="space-y-4">
                      {run.users.length > 1 && (
                        <UserTabs
                          results={ordered}
                          selected={selected.slug}
                          onSelect={setSelectedSlug}
                        />
                      )}
                      <Card className={failed ? "border-destructive/50" : ""}>
                        <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
                          <CardTitle className="flex items-center gap-2.5">
                            <UserAvatar name={selected.username} size="sm" />
                            {userId !== null ? (
                              <Link
                                to={`/users/${userId}`}
                                className="rounded-sm hover:text-primary hover:underline"
                              >
                                {selected.username}
                              </Link>
                            ) : (
                              selected.username
                            )}
                            <Badge
                              variant={
                                failed
                                  ? "destructive"
                                  : runStatusVariant(selected.status)
                              }
                            >
                              {runStatusLabel(selected.status)}
                            </Badge>
                          </CardTitle>
                          <p className="text-sm text-muted-foreground">
                            {formatDuration(selected.duration_ms)}
                            {selected.llm_tokens > 0
                              ? ` · ${selected.llm_tokens.toLocaleString()} AI tokens`
                              : ""}
                          </p>
                        </CardHeader>
                        <CardContent>
                          <UserPanel run={run} result={selected} />
                        </CardContent>
                      </Card>
                    </div>
                  );
                })()
              )}

              {(liveLog.length > 0 || !run.finished_at) && (
                <ActivityLog entries={liveLog} running={!run.finished_at} />
              )}
            </div>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
