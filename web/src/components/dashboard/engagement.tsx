import { Eye, TrendingDown } from "lucide-react";

import { QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { timeAgo } from "@/lib/format";
import { useEngagement } from "@/lib/queries";
import type {
  EngagementPick,
  EngagementReport,
  ReportWindow,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type Outcome = { label: string; className: string };

/** Credited but no live session ever said how far — the state every pre-tracking pick is in. */
const WATCHING: Outcome = { label: "Watching", className: "text-primary" };

const OUTCOME: Record<string, Outcome> = {
  finished: { label: "Finished", className: "text-success" },
  dropped: { label: "Dropped", className: "text-primary" },
  bounced: { label: "Bounced", className: "text-destructive-text" },
  watching: WATCHING,
};

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="min-w-0">
      <CardContent className="space-y-3 pt-6">
        <div>
          <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
          {hint && (
            <p className="mt-0.5 text-xs text-muted-foreground/80">{hint}</p>
          )}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

/**
 * How far one pick got, as a bar.
 *
 * Same two-intensity amber the dashboard already uses for watched-vs-finished, because these are
 * ordered rather than categorical. A bounce is the exception and gets the destructive tone: it is
 * not "less finished", it is a different thing — the pick was wrong.
 */
function ProgressBar({ pick }: { pick: EngagementPick }) {
  // No bar at all when the progress is UNKNOWN — which is every pick from before tracking, and every
  // show. A zero-width bar is a claim that they got nowhere, three lines from the comment insisting
  // an em-dash is used for exactly that reason.
  if (pick.percent === null && pick.outcome !== "finished") {
    return (
      <div
        className="h-1.5 w-full rounded-full bg-muted/50"
        aria-label="how far they got was not observed"
      />
    );
  }
  const percent = pick.percent ?? 100;
  const tone =
    pick.outcome === "finished"
      ? "bg-primary"
      : pick.outcome === "bounced"
        ? "bg-destructive"
        : "bg-primary/50";
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div className={cn("h-full", tone)} style={{ width: `${percent}%` }} />
    </div>
  );
}

function PickLine({ pick }: { pick: EngagementPick }) {
  const outcome = OUTCOME[pick.outcome] ?? WATCHING;
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5 sm:flex-nowrap">
      <span
        className={cn(
          "w-20 shrink-0 text-xs font-medium uppercase tracking-wide",
          outcome.className,
        )}
      >
        {outcome.label}
      </span>
      <span className="min-w-0 flex-grow truncate text-sm">{pick.title}</span>
      <Badge variant="secondary" className="shrink-0 font-normal">
        {pick.row}
      </Badge>
      <div className="hidden w-32 shrink-0 xl:block">
        <ProgressBar pick={pick} />
      </div>
      <span className="w-12 shrink-0 text-right text-sm tabular-nums text-muted-foreground">
        {/* An em-dash, not 0%: a pick with no percentage was never watched happening. Plex cannot
            see a partial play, so "unknown" and "they bailed instantly" are different facts and
            printing 0 would turn the first into the second. */}
        {pick.percent === null ? "—" : `${pick.percent}%`}
      </span>
      {/* Always rendered, so the columns line up. Dropping the span for a session-only pick left
          those rows 96px shorter than their neighbours — in a list where they are the interesting
          ones. */}
      <span className="w-24 shrink-0 text-right text-xs text-muted-foreground">
        {pick.watched_at ? timeAgo(pick.watched_at) : "—"}
      </span>
    </li>
  );
}

function People({ people }: { people: EngagementReport["people"] }) {
  return (
    <Section
      title="What people did with their picks"
      hint="Every pick they started, and how far it got. Busiest first."
    >
      <div className="space-y-5">
        {people.map((person) => (
          <div key={person.username}>
            <div className="mb-1 flex items-baseline gap-2">
              <span className="text-sm font-medium">
                {person.display_name || person.username}
              </span>
              <span className="text-xs text-muted-foreground">
                {person.total > person.picks.length
                  ? `${person.picks.length} of ${person.total} picks`
                  : `${person.total} ${person.total === 1 ? "pick" : "picks"}`}
              </span>
            </div>
            <ul className="space-y-0.5">
              {person.picks.map((pick, i) => (
                <PickLine key={`${pick.title}-${i}`} pick={pick} />
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Losing({ losing }: { losing: EngagementReport["losing"] }) {
  return (
    <Section
      title="Picks that lose people"
      hint="Started by two or more people and finished by few. The number is where they typically stop."
    >
      <ul className="space-y-0.5">
        {losing.map((title, i) => (
          <li
            key={`${title.title}-${i}`}
            className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md px-2 py-1.5"
          >
            <span className="min-w-0 flex-grow truncate text-sm">
              {title.title}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {title.started} started · {title.finished} finished
            </span>
            <span
              className={cn(
                "w-24 shrink-0 text-right text-sm tabular-nums",
                (title.stops_at ?? 100) < 25
                  ? "text-destructive-text"
                  : "text-muted-foreground",
              )}
            >
              {title.stops_at === null ? "—" : `stops at ${title.stops_at}%`}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-xs leading-relaxed text-muted-foreground/80">
        An early stop is a pick problem. A late stop is usually the title rather
        than the recommendation — people got most of the way through and
        something else came up.
      </p>
    </Section>
  );
}

function StopPoints({ points }: { points: EngagementReport["stop_points"] }) {
  const max = Math.max(1, ...points.map((p) => p.count));
  return (
    <Section
      title="Where people stop"
      hint="Every abandoned start, by how far in."
    >
      <div className="flex h-28 items-end gap-1.5">
        {points.map((point) => (
          <div
            key={point.label}
            className="flex flex-grow flex-col items-center gap-1.5"
          >
            <span className="text-xs tabular-nums text-muted-foreground">
              {point.count || ""}
            </span>
            <div
              className={cn(
                "w-full rounded-t-sm",
                point.label === "0-10%" ? "bg-destructive" : "bg-primary/50",
              )}
              style={{ height: `${Math.max(2, (point.count / max) * 76)}px` }}
            />
            <span className="text-xs text-muted-foreground">{point.label}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}

/**
 * The detail behind the dashboard's Dropped tile.
 *
 * Everything here comes from live playback, so all four states are empty until the listener has
 * observed some — which is why the empty state says that rather than rendering zeroes. A page of
 * zeroes reads as "nobody watches anything", when the truth is "we have not been watching yet".
 */
export function Engagement({ reportWindow }: { reportWindow: ReportWindow }) {
  const engagement = useEngagement(reportWindow);
  return (
    <QueryBoundary
      query={engagement}
      skeleton={<Skeleton className="h-64 w-full" />}
    >
      {(data) =>
        !data.observed ? (
          <Section title="What people did with their picks">
            <div className="flex items-start gap-3 py-2 text-sm text-muted-foreground">
              <Eye
                className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
              <p className="leading-relaxed">
                No playback observed yet in this window. Watch tracking records
                plays as they happen, so this fills in from the first time
                someone opens a pick — there is no history to backfill it from,
                because Plex does not record how far anyone got. Films only:
                how far through an episode someone got does not tell us how far
                through a series they are.
              </p>
            </div>
          </Section>
        ) : (
          <div className="space-y-4">
            <People people={data.people} />
            <div className="grid gap-4 xl:grid-cols-2">
              {data.losing.length > 0 ? (
                <Losing losing={data.losing} />
              ) : (
                <Section
                  title="Picks that lose people"
                  hint="Started by two or more people and finished by few."
                >
                  <div className="flex items-start gap-3 py-2 text-sm text-muted-foreground">
                    <TrendingDown
                      className="mt-0.5 h-4 w-4 shrink-0"
                      aria-hidden="true"
                    />
                    <p className="leading-relaxed">
                      No title has lost more than one person yet. One person
                      abandoning something is a night, not a signal — this needs
                      a pattern across people before it means anything.
                    </p>
                  </div>
                </Section>
              )}
              <StopPoints points={data.stop_points} />
            </div>
          </div>
        )
      }
    </QueryBoundary>
  );
}
