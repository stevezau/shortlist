import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Copy } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatDuration, runStatusVariant } from "@/lib/format";
import { queryKeys, useRun } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { RunDetail, RunUserResult } from "@/lib/types";

function githubIssueSnippet(run: RunDetail, result: RunUserResult): string {
  return [
    "### Rowarr run error",
    "",
    `- Run: #${run.id} (${run.trigger}${run.dry_run ? ", dry-run" : ""})`,
    `- Started: ${run.started_at}`,
    `- User: ${result.slug}`,
    `- Status: ${result.status}`,
    "",
    "```",
    result.error ?? "(no error message)",
    "```",
  ].join("\n");
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

function DiffList({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: string;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className={`text-xs font-semibold uppercase tracking-wide ${tone}`}>
        {label} ({items.length})
      </p>
      <p className="text-sm text-muted-foreground">{items.join(" · ")}</p>
    </div>
  );
}

function UserResultCard({
  run,
  result,
}: {
  run: RunDetail;
  result: RunUserResult;
}) {
  const failed = result.error !== null;
  // A user the run left alone comes back with `diff: {}` — not three empty lists.
  const added = result.diff.added ?? [];
  const removed = result.diff.removed ?? [];
  const kept = result.diff.kept ?? [];
  return (
    <Card className={failed ? "border-destructive/50" : ""}>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2">
          <Link to="/users" className="rounded-sm hover:underline">
            {result.username}
          </Link>
          <Badge
            variant={failed ? "destructive" : runStatusVariant(result.status)}
          >
            {result.status}
          </Badge>
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          {formatDuration(result.duration_ms)} ·{" "}
          {result.llm_tokens.toLocaleString()} LLM tokens
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {failed ? (
          <div
            role="alert"
            className="space-y-3 rounded-md bg-destructive/10 p-3"
          >
            <p className="font-mono text-sm text-destructive-foreground">
              {result.error}
            </p>
            <CopyForGitHubButton run={run} result={result} />
          </div>
        ) : (
          <>
            <DiffList label="Added" items={added} tone="text-success" />
            <DiffList label="Removed" items={removed} tone="text-destructive" />
            <DiffList label="Kept" items={kept} tone="text-muted-foreground" />
            {added.length === 0 &&
              removed.length === 0 &&
              kept.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  No changes — the row was already up to date.
                </p>
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
  const queryClient = useQueryClient();

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
      <Link
        to="/runs"
        className="inline-flex items-center gap-1 rounded-sm text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        All runs
      </Link>

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
              </p>
            </header>

            {run.users.length === 0 ? (
              <EmptyState
                title="No per-user results yet"
                hint="This run hasn't processed any users so far. Results appear here as each user finishes."
              />
            ) : (
              <div className="space-y-4">
                {run.users.map((result) => (
                  <UserResultCard key={result.slug} run={run} result={result} />
                ))}
              </div>
            )}
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
