import { Link } from "react-router";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, runStatusVariant } from "@/lib/format";
import { useUserRuns, useUserRunsSummary } from "@/lib/queries";
import type { UserRun } from "@/lib/types";

/** How this person fared in a run, in their words — not the run's.
 *
 *  A run is server-wide: its status says whether the RUN succeeded, which tells you nothing about
 *  whether this person got a row. `skipped` with a reason is a normal, healthy outcome and must not
 *  wear the same red as `error`. */
const OUTCOME: Record<string, { label: string; tone: "ok" | "muted" | "bad" }> =
  {
    ok: { label: "built", tone: "ok" },
    cold_start: { label: "cold start", tone: "muted" },
    skipped: { label: "skipped", tone: "muted" },
    error: { label: "failed", tone: "bad" },
    pending: { label: "pending", tone: "muted" },
  };

function duration(ms: number | null): string | null {
  if (!ms) return null;
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function RunLine({ run, userSlug }: { run: UserRun; userSlug: string }) {
  const added = run.diff.added?.length ?? 0;
  const removed = run.diff.removed?.length ?? 0;
  const outcome = OUTCOME[run.status] ?? { label: run.status, tone: "muted" };
  const took = duration(run.duration_ms);

  return (
    <li className="rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Link
            // Deep-link to this person's panel, not the top of a run with forty others in it.
            to={`/runs/${run.run_id}?user=${encodeURIComponent(userSlug)}`}
            className="font-medium hover:text-primary hover:underline"
          >
            Run #{run.run_id}
          </Link>
          <Badge variant={runStatusVariant(run.status)}>{outcome.label}</Badge>
          {run.dry_run && (
            <Badge
              variant="outline"
              title="A rehearsal — nothing was written to Plex."
            >
              Test run
            </Badge>
          )}
        </div>
        <span className="text-xs text-muted-foreground">
          {run.finished_at
            ? formatDate(run.finished_at)
            : formatDate(run.started_at)}
          {took && ` · ${took}`}
        </span>
      </div>
      {run.error ? (
        <div className="mt-1 space-y-1">
          <p className="text-xs text-muted-foreground">
            Something went wrong building this person&rsquo;s row — copy the
            details below when reporting it:
          </p>
          <p className="font-mono text-xs text-destructive-text">{run.error}</p>
        </div>
      ) : run.reason ? (
        // Not a failure — the run deliberately left this person out, and said why.
        <p className="mt-1 text-xs text-muted-foreground">{run.reason}</p>
      ) : (
        <p className="mt-1 text-xs text-muted-foreground">
          +{added} new · −{removed} rotated out · {run.picks.length} picks
        </p>
      )}
    </li>
  );
}

/**
 * The runs that included this person, each showing THEIR outcome.
 *
 * Runs are server-wide, so this is deliberately explicit about being a filtered view: the footer
 * names how many runs it isn't showing, otherwise six entries read as "the server has run 6 times".
 */
export function RecentRuns({
  userId,
  userSlug,
}: {
  userId: number;
  userSlug: string;
}) {
  const query = useUserRuns(userId);
  const summary = useUserRunsSummary(userId);
  const others = summary.data
    ? Math.max(0, summary.data.total - summary.data.included)
    : 0;

  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-32 w-full" />}
      isEmpty={(runs) => runs.length === 0}
      empty={
        <EmptyState
          title="No runs have included this person yet"
          hint="Once a run processes them, each one shows up here with what it changed and why."
        />
      }
    >
      {(runs) => (
        <div className="space-y-3">
          <ul className="space-y-2">
            {runs.map((run) => (
              <RunLine key={run.run_id} run={run} userSlug={userSlug} />
            ))}
          </ul>
          {others > 0 && (
            <p className="text-xs text-muted-foreground">
              {others} other {others === 1 ? "run" : "runs"} didn&rsquo;t
              include this person.{" "}
              <Link
                to="/runs"
                className="underline underline-offset-2 hover:text-foreground"
              >
                See all runs
              </Link>
            </p>
          )}
        </div>
      )}
    </QueryBoundary>
  );
}
