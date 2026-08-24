import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { QueryBoundary } from "@/components/query-boundary";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useEngagement } from "@/lib/queries";
import type {
  EffectivenessReport,
  EngagementPick,
  EngagementReport,
  ReportWindow,
} from "@/lib/types";

/**
 * What is NOT working, and nothing else.
 *
 * This replaced three cards — a per-person breakdown of every pick, a "titles that lose people"
 * table and a histogram of stop points. Together they filled a screen to deliver, on a real 47-user
 * server, one fact: somebody gave up on one film. The per-person grouping was the worst of it: a
 * directory of twenty-five names answering "who watches things", which the By person card above
 * already answers, with the one interesting row buried inside it.
 *
 * The question this page exists to answer is "is my setup working", and the half of that nothing
 * else on the page addresses is "what ISN'T". So: the problems, as a short list, each one a thing
 * the owner could act on. When there are none, it says so in a line and takes no space.
 */

type Problem = {
  key: string;
  text: React.ReactNode;
  /** What to do about it, when there is something. */
  hint?: string;
};

/** People who were given picks and watched none of them — the biggest silent failure there is. */
function idlePeople(coverage: EffectivenessReport["coverage"]): Problem | null {
  const idle = coverage.users_with_picks - coverage.users_watched;
  if (idle <= 0) return null;
  return {
    key: "idle",
    text: (
      <>
        <strong className="font-medium text-foreground">{idle}</strong> of{" "}
        {coverage.users_with_picks} people got picks and watched none of them
      </>
    ),
    hint:
      idle >= coverage.users_with_picks / 2
        ? "More than half. That usually means the row is not where they look, rather than that the picks are wrong."
        : undefined,
  };
}

/** A row that delivered and landed nothing. The clearest "this row is not earning its place". */
function deadRows(rows: EffectivenessReport["per_row"]): Problem[] {
  return rows
    .filter((r) => !r.deleted && r.delivered >= 20 && r.watched === 0)
    .slice(0, 3)
    .map((r) => ({
      key: `dead-${r.slug}-${r.library}`,
      text: (
        <>
          <strong className="font-medium text-foreground">{r.name}</strong>{" "}
          delivered {r.delivered} picks and none were watched
        </>
      ),
    }));
}

/**
 * Titles fetched for people that nobody then watched.
 *
 * Deliberately NOT a flag for "a row landed picks but finished none". A series only counts as
 * finished when every episode is watched, so a TV row sitting on zero finishes is the normal case
 * (21 of 158 credited show picks on a real server), and flagging it would fire every day on every
 * server — which is how a list like this stops being read.
 */
function unwatchedRequests(
  requests: EffectivenessReport["requests"],
): Problem | null {
  if (requests.sent < 5 || requests.watched_after_sent > 0) return null;
  return {
    key: "requests",
    text: (
      <>
        <strong className="font-medium text-foreground">{requests.sent}</strong>{" "}
        titles were fetched for people and none have been watched since
      </>
    ),
    hint: "Worth checking they actually arrived, and that the row picked them up afterwards.",
  };
}

/** Somebody started a pick and gave up. The one signal Plex's own watched flag cannot give. */
function gaveUp(people: EngagementReport["people"]): Problem[] {
  const out: { person: string; pick: EngagementPick }[] = [];
  for (const person of people) {
    for (const pick of person.picks) {
      if (pick.outcome === "bounced" || pick.outcome === "dropped") {
        out.push({ person: person.display_name || person.username, pick });
      }
    }
  }
  // Furthest-from-finishing first: someone who bailed at 3% is a worse pick than one who got to 70%.
  out.sort((a, b) => (a.pick.percent ?? 100) - (b.pick.percent ?? 100));
  return out.slice(0, 5).map(({ person, pick }, i) => ({
    key: `gave-up-${pick.title}-${i}`,
    text: (
      <>
        <strong className="font-medium text-foreground">{person}</strong> gave
        up on <span className="text-foreground">{pick.title}</span>
        {pick.percent !== null && ` after ${pick.percent}%`}
        <span className="text-muted-foreground/70"> · {pick.row}</span>
      </>
    ),
  }));
}

export function NeedsALook({
  report,
  reportWindow,
}: {
  report: EffectivenessReport;
  reportWindow: ReportWindow;
}) {
  const engagement = useEngagement(reportWindow);
  return (
    <Card className="min-w-0">
      <CardContent className="pt-6">
        <h2 className="text-sm font-medium text-muted-foreground">
          Worth a look
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground/80">
          Where the picks are not landing.
        </p>
        <QueryBoundary
          query={engagement}
          skeleton={<Skeleton className="mt-3 h-16 w-full" />}
        >
          {(data) => {
            const problems = [
              idlePeople(report.coverage),
              ...deadRows(report.per_row),
              unwatchedRequests(report.requests),
              ...gaveUp(data.people),
            ].filter((p): p is Problem => p !== null);
            if (problems.length === 0) {
              return (
                <p className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
                  <CheckCircle2
                    className="h-4 w-4 shrink-0 text-success"
                    aria-hidden="true"
                  />
                  Everyone who got a pick watched something, and no row came up
                  empty.
                </p>
              );
            }
            return (
              <ul className="mt-3 space-y-2">
                {problems.map((problem) => (
                  <li key={problem.key} className="flex items-start gap-2.5">
                    <AlertTriangle
                      className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary/70"
                      aria-hidden="true"
                    />
                    <div className="min-w-0 text-sm text-muted-foreground">
                      <p className="leading-snug">{problem.text}</p>
                      {problem.hint && (
                        <p className="mt-0.5 text-xs leading-snug text-muted-foreground/70">
                          {problem.hint}
                        </p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            );
          }}
        </QueryBoundary>
      </CardContent>
    </Card>
  );
}
