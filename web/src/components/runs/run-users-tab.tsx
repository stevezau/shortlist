import { AlertCircle, Telescope } from "lucide-react";
import { Link } from "react-router";

import { UserAvatar } from "@/components/user-avatar";
import { UserPanel } from "@/components/runs/user-panel";
import { UserTabs } from "@/components/runs/user-tabs";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import {
  errorBucket,
  exaSummary,
  friendlyError,
  tokenStepBreakdown,
} from "@/lib/run-format";
import { cn } from "@/lib/utils";
import type { RunDetail, RunLogEntry } from "@/lib/types";

/** " (final picks 12,340 · web search 4,100)" for a by-step token map, or "" when empty. */
function tokenStepSummary(byStep?: Record<string, number>): string {
  const parts = tokenStepBreakdown(byStep);
  return parts ? ` (${parts})` : "";
}

/**
 * The People tab: the failures-first user nav on the left, the selected person's rows on the right,
 * with a "N people failed with the same problem" banner up top when that's genuinely true.
 *
 * Assumes `run.users.length > 0` — the caller renders its own empty state otherwise.
 */
export function RunUsersTab({
  run,
  selectedSlug,
  onSelect,
  idBySlug,
  liveLog,
}: {
  run: RunDetail;
  selectedSlug: string;
  onSelect: (slug: string) => void;
  idBySlug: Map<string, number>;
  liveLog: RunLogEntry[];
}) {
  // Failures first in the nav, so a partly-failed run opens on the error you came for.
  const ordered = [...run.users].sort(
    (a, b) => Number(b.error !== null) - Number(a.error !== null),
  );
  const selected = run.users.find((u) => u.slug === selectedSlug) ?? ordered[0];
  if (!selected) return null;
  const failed = selected.error !== null;
  const userId = idBySlug.get(selected.slug) ?? null;

  // When several people failed the SAME recognised way (a Plex outage, a shared 429), say it once up
  // top so you don't click through dozens of identical errors. Two unrelated, unrecognised failures
  // must never be announced as "the same problem" — errorBucket returns null for those, and a null
  // bucket is never counted here.
  const buckets = new Map<string, { count: number; msg: string }>();
  for (const u of run.users) {
    if (!u.error) continue;
    const bucket = errorBucket(u.error);
    if (!bucket) continue;
    const existing = buckets.get(bucket);
    buckets.set(bucket, {
      count: (existing?.count ?? 0) + 1,
      msg: friendlyError(u.error),
    });
  }
  const topError = [...buckets.values()].sort((a, b) => b.count - a.count)[0];
  const commonError = topError && topError.count >= 2 ? topError : null;

  return (
    <div className="space-y-4">
      {commonError && (
        <div
          role="alert"
          className="flex gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
        >
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
          <p>
            <span className="font-medium">
              {commonError.count} people failed with the same problem.
            </span>{" "}
            {commonError.msg} Open any person below for the raw details.
          </p>
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)] lg:items-start">
        <div className="lg:sticky lg:top-4">
          <UserTabs
            results={ordered}
            selected={selected.slug}
            onSelect={onSelect}
          />
        </div>
        <Card className={cn("min-w-0", failed && "border-destructive/50")}>
          <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="flex items-center gap-2.5">
              <UserAvatar name={selected.username} size="sm" />
              {userId !== null ? (
                <Link
                  to={`/users/${userId}`}
                  className="rounded-sm hover:text-primary hover:underline"
                >
                  {selected.display_name || selected.username}
                </Link>
              ) : (
                selected.display_name || selected.username
              )}
              <Badge
                variant={
                  failed ? "destructive" : runStatusVariant(selected.status)
                }
              >
                {runStatusLabel(selected.status)}
              </Badge>
            </CardTitle>
            <div className="flex items-center gap-3">
              <p className="text-sm text-muted-foreground">
                {formatDuration(selected.duration_ms)}
                {selected.llm_tokens > 0
                  ? ` · ${selected.llm_tokens.toLocaleString()} AI tokens${tokenStepSummary(
                      selected.llm_tokens_by_step,
                    )}`
                  : ""}
                {exaSummary(selected.exa_searches)}
              </p>
              {selected.has_trace && userId !== null && (
                <Button
                  asChild
                  variant="secondary"
                  size="sm"
                  className="gap-1.5 border border-primary/30 bg-primary/10 text-primary hover:bg-primary/20"
                >
                  <Link to={`/runs/${run.id}/trace/${userId}`}>
                    <Telescope className="h-3.5 w-3.5" aria-hidden="true" />
                    How we picked
                  </Link>
                </Button>
              )}
            </div>
          </CardHeader>
          <CardContent>
            <UserPanel run={run} result={selected} liveLog={liveLog} />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
