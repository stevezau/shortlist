import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { PickList } from "@/components/pick-list";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { formatDate, formatDuration, runStatusVariant } from "@/lib/format";
import { githubIssueSnippet } from "@/lib/github";
import { queryKeys, useRun, useUsers } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type {
  RunDetail,
  RunLibraryBreakdown,
  RunUserResult,
} from "@/lib/types";

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

const DIFF_TONES = {
  added: {
    label: "text-success",
    chip: "border-success/30 bg-success/10 text-success",
  },
  removed: {
    label: "text-destructive",
    chip: "border-destructive/30 bg-destructive/10 text-destructive",
  },
  kept: {
    label: "text-muted-foreground",
    chip: "border-border bg-muted text-muted-foreground",
  },
} as const;

function DiffChips({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: keyof typeof DIFF_TONES;
}) {
  if (items.length === 0) return null;
  const styles = DIFF_TONES[tone];
  return (
    <div className="space-y-1.5">
      <p
        className={cn(
          "text-xs font-semibold uppercase tracking-wide",
          styles.label,
        )}
      >
        {label} ({items.length})
      </p>
      <ul className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <li
            key={item}
            className={cn(
              "rounded-md border px-2 py-0.5 text-xs font-medium",
              styles.chip,
            )}
          >
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** One library's slice of a row: what changed there + that library's own ranked picks. */
function LibraryBlock({ entry }: { entry: RunLibraryBreakdown }) {
  const touched =
    entry.added.length +
      entry.removed.length +
      entry.kept.length +
      entry.deleted.length >
    0;
  return (
    <div className="space-y-2 rounded-md border border-border/60 p-3">
      <div className="flex items-center gap-2">
        <Badge variant="outline">{entry.library_title}</Badge>
        <span className="text-xs text-muted-foreground">
          {entry.picks.length} pick{entry.picks.length === 1 ? "" : "s"}
        </span>
      </div>
      <DiffChips label="Added" items={entry.added} tone="added" />
      <DiffChips label="Removed" items={entry.removed} tone="removed" />
      <DiffChips label="Kept" items={entry.kept} tone="kept" />
      <DiffChips label="Rows deleted" items={entry.deleted} tone="removed" />
      {!touched && (
        <p className="text-sm text-muted-foreground">
          No changes — this library’s row was already up to date.
        </p>
      )}
      {entry.picks.length > 0 && (
        <PickList picks={entry.picks} className="mt-1" />
      )}
    </div>
  );
}

/** Group a user's per-(row, library) breakdown by row, so each row shows its libraries together. */
function BreakdownView({ breakdown }: { breakdown: RunLibraryBreakdown[] }) {
  const rows = new Map<string, RunLibraryBreakdown[]>();
  for (const entry of breakdown) {
    const list = rows.get(entry.row_slug) ?? [];
    list.push(entry);
    rows.set(entry.row_slug, list);
  }
  return (
    <div className="space-y-4">
      {[...rows.values()].map((entries) => {
        const head = entries[0];
        if (!head) return null;
        return (
          <div key={head.row_slug} className="space-y-2">
            <p className="text-sm font-semibold">{head.row_title}</p>
            <div className="space-y-2">
              {entries.map((entry) => (
                <LibraryBlock key={entry.library_key} entry={entry} />
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function UserResultCard({
  run,
  result,
  userId,
}: {
  run: RunDetail;
  result: RunUserResult;
  /** Numeric id of this user for a deep-link, or null when they're no longer on the server. */
  userId: number | null;
}) {
  const failed = result.error !== null;
  // A user the run left alone comes back with `diff: {}` — not three empty lists.
  const added = result.diff.added ?? [];
  const removed = result.diff.removed ?? [];
  const kept = result.diff.kept ?? [];
  // Deleting a whole row is the most destructive thing a run does. It has to be on this page:
  // "what changed on whose share at 03:31" must always be answerable from the UI.
  const deleted = result.diff.deleted ?? [];
  return (
    <Card className={failed ? "border-destructive/50" : ""}>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2.5">
          <UserAvatar name={result.username} size="sm" />
          {userId !== null ? (
            <Link
              to={`/users/${userId}`}
              className="rounded-sm hover:text-primary hover:underline"
            >
              {result.username}
            </Link>
          ) : (
            result.username
          )}
          <Badge
            variant={failed ? "destructive" : runStatusVariant(result.status)}
          >
            {result.status}
          </Badge>
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          {formatDuration(result.duration_ms)}
          {result.llm_tokens > 0
            ? ` · ${result.llm_tokens.toLocaleString()} AI tokens`
            : ""}
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {failed ? (
          <div
            role="alert"
            className="space-y-3 rounded-md bg-destructive/10 p-3"
          >
            <p className="font-mono text-sm text-destructive">{result.error}</p>
            <CopyForGitHubButton run={run} result={result} />
          </div>
        ) : result.breakdown.length > 0 ? (
          // Per-(row, library) — each library shows what changed there and its OWN ranked picks,
          // so a row spanning Movies + TV reads as two clear groups, not one merged list.
          <BreakdownView breakdown={result.breakdown} />
        ) : (
          // Legacy runs (before the breakdown was recorded): the merged diff + flat pick list.
          <>
            <DiffChips label="Added" items={added} tone="added" />
            <DiffChips label="Removed" items={removed} tone="removed" />
            <DiffChips label="Kept" items={kept} tone="kept" />
            <DiffChips label="Rows deleted" items={deleted} tone="removed" />
            {added.length === 0 &&
              removed.length === 0 &&
              kept.length === 0 &&
              deleted.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No changes — the row was already up to date.
                </p>
              )}
            {result.picks.length > 0 && (
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Picks ({result.picks.length})
                </p>
                <PickList picks={result.picks} className="mt-1" />
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
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

  // Keep an in-flight run's page live: refetch on every stage/finish event.
  // run.user.stage carries no run_id, so any stage event refreshes this page
  // (only the newest run can be in flight anyway).
  useSSE({
    onRunUserStage: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
    },
    onRunFinished: (event) => {
      if (event.run_id === runId) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
        void queryClient.invalidateQueries({ queryKey: queryKeys.runs });
      }
    },
  });

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
                    {run.status}
                  </Badge>
                  {run.dry_run && (
                    <Badge variant="outline">
                      dry-run — nothing was written to Plex
                    </Badge>
                  )}
                </div>
                <p className="text-sm text-muted-foreground">
                  {run.trigger} · started {formatDate(run.started_at)}
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
                  title="No per-user results yet"
                  hint="This run hasn't processed any users so far. Results appear here as each user finishes."
                />
              ) : (
                <div className="space-y-4">
                  {/* Failures first — when a run partly fails, the thing you opened this page to see
                    is the error, not the twelve users that succeeded above it. */}
                  {[...run.users]
                    .sort(
                      (a, b) =>
                        Number(b.error !== null) - Number(a.error !== null),
                    )
                    .map((result) => (
                      <UserResultCard
                        key={result.slug}
                        run={run}
                        result={result}
                        userId={idBySlug.get(result.slug) ?? null}
                      />
                    ))}
                </div>
              )}
            </div>
          )}
        </QueryBoundary>
      )}
    </div>
  );
}
