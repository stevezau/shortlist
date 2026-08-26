import { Why } from "@/components/dashboard/engagement";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useUserOutcomes } from "@/lib/queries";
import type { UserPickOutcome } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * What this person did with the picks Shortlist gave them.
 *
 * The page could already show what was DELIVERED (their rows) and what they had watched on Plex
 * (their history), but not the join of the two — whether the recommendations were actually seen out.
 * That is the one question the whole feature exists to answer, and until this it could only be asked
 * of the server as a whole.
 *
 * Four outcomes, and the wording of each matters more than the layout. "Still watching" is not a
 * hedge: the server refuses to call a watch abandoned while it is open or less than a day old, so a
 * verdict this page cannot support is not printed (see `SETTLING_HOURS`).
 */
const WHY = {
  gaveUp:
    "A film they started and have not played again for 24 hours. The clock restarts if they come back to it, so this can change back.",
  watching:
    "Either still playing, or stopped too recently to call — and a series is always here, because one episode says nothing about a whole show.",
} as const;

const LABEL: Record<string, { text: string; className: string; why?: string }> = {
  finished: { text: "Finished", className: "text-success" },
  dropped: { text: "Gave up part-way", className: "text-destructive-text", why: WHY.gaveUp },
  bounced: { text: "Barely started", className: "text-destructive-text", why: WHY.gaveUp },
  watching: { text: "Still watching", className: "text-muted-foreground", why: WHY.watching },
};

function Line({ pick }: { pick: UserPickOutcome }) {
  const label = LABEL[pick.outcome] ?? LABEL.watching;
  return (
    <li className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm">
      <span className="min-w-0 flex-1 truncate text-foreground">
        {pick.title}
      </span>
      <span className={cn("font-medium tabular-nums", label?.className)}>
        {label?.text}
        {/* The percentage only where it adds something. On a finished title it is noise, and it is
            always null for a series — an episode's progress is not the show's. */}
        {pick.outcome !== "finished" && pick.percent !== null && (
          <span className="font-normal text-muted-foreground">
            {" "}
            · {pick.percent}%
          </span>
        )}
      </span>
      {label?.why && <Why text={label.why} />}
      <Badge variant="secondary" className="font-normal">
        {pick.row}
      </Badge>
      {pick.watched_at && (
        <span className="text-muted-foreground">· {timeAgo(pick.watched_at)}</span>
      )}
    </li>
  );
}

export function PickOutcomes({ userId }: { userId: number }) {
  const query = useUserOutcomes(userId);
  return (
    <QueryBoundary query={query} skeleton={<Skeleton className="h-32 w-full" />}>
      {(picks) => {
        if (picks.length === 0) {
          return (
            <EmptyState
              title="Nothing watched yet"
              // Says which of the two reasons it is, because they need opposite responses: a row
              // that never delivered is a setup problem, a row that delivered and was ignored is a
              // recommendation problem. This page cannot tell them apart, so it does not pretend to.
              hint="Once they play something Shortlist put in one of their rows, it shows up here with how far they got."
            />
          );
        }
        const finished = picks.filter((p) => p.outcome === "finished").length;
        const partial = picks.filter(
          (p) => p.outcome === "dropped" || p.outcome === "bounced",
        ).length;
        return (
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground tabular-nums">
                {picks.length}
              </span>{" "}
              picks played ·{" "}
              <span className="font-medium text-success tabular-nums">
                {finished}
              </span>{" "}
              finished
              {/* Only when there is one — a line that says "0 gave up" every day teaches you to stop
                  reading the number on the day it is not zero. */}
              {partial > 0 && (
                <>
                  {" · "}
                  <span className="font-medium text-destructive-text tabular-nums">
                    {partial}
                  </span>{" "}
                  not finished
                </>
              )}
            </p>
            <ul className="space-y-1.5">
              {picks.map((pick) => (
                <Line key={`${pick.tmdb_id}-${pick.media_type}`} pick={pick} />
              ))}
            </ul>
          </div>
        );
      }}
    </QueryBoundary>
  );
}
