import { AlertTriangle } from "lucide-react";

import { panelRowClass, ReportPanel } from "@/components/dashboard/report-panel";
import { QueryBoundary } from "@/components/query-boundary";
import { Why } from "@/components/why";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useEngagement } from "@/lib/queries";
import type {
  EffectivenessReport,
  EngagementPick,
  EngagementReport,
  ReportWindow,
} from "@/lib/types";
import { personName } from "@/lib/user-names";

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

/** Mirrors `SETTLING_HOURS` server-side (`report_service`), which decides the same thing. Displayed
 *  only — the server owns the rule; this is the sentence that explains it. */
const SETTLED_AFTER_HOURS = 24;

/** Mirrors `BOUNCE_PERCENT` server-side, which decides which picks are `bounced` and therefore which
 *  ones this card leaves out. Same arrangement as `SETTLED_AFTER_HOURS`: the server owns the rule,
 *  this is the number the sentences quote. Written out twice as a bare "5%" first, which is the
 *  drift this file already had a named constant to prevent. */
const BOUNCE_FLOOR_PERCENT = 5;

/** When the app is willing to call something a give-up AT ALL. Exported so the verdict card and this
 *  one cannot drift into saying different things about the same rule.
 *
 *  The two cards no longer COUNT the same set — the verdict tile totals every abandonment, the
 *  findings list below leaves out the ones under 5% — so they no longer share one sentence either.
 *  `WHY_GAVE_UP_FINDING` is this rule plus that floor, and it is the only one the findings card
 *  shows. Handing both cards the identical sentence while one of them silently applied an extra
 *  filter is precisely the drift this constant exists to prevent, and it read as a contradiction:
 *  "48 gave up part-way" above a list containing none of them. */
export const WHY_GAVE_UP = `Only films, and only after ${SETTLED_AFTER_HOURS}h with no further play — the clock restarts if they come back, and a series is never counted here.`;

/** The findings list's version: the same rule, plus why the shortest ones are missing from it. */
export const WHY_GAVE_UP_FINDING = `${WHY_GAVE_UP} Ones under ${BOUNCE_FLOOR_PERCENT}% in are counted above but not listed here — that is too little to tell a wrong pick from a mis-click.`;

type Problem = {
  key: string;
  text: React.ReactNode;
  /** What to do about it, when there is something. */
  hint?: string;
};

/** People who were given picks and watched none of them — the biggest silent failure there is. */
function idlePeople(coverage: EffectivenessReport["coverage"]): Problem | null {
  // Straight from the API. Deriving it as `users_with_picks - users_watched` subtracted two
  // differently-scoped populations — the second counts anyone who watched in the window, including
  // someone whose pick landed last month — so it could reach zero while people who got picks this
  // week had watched nothing, and the card would then claim everyone had watched something.
  const idle = coverage.users_idle;
  if (idle <= 0) return null;
  return {
    key: "idle",
    text: (
      <>
        <strong className="font-medium text-foreground">{idle}</strong> of the{" "}
        {coverage.users_with_picks} people who got picks watched none of them
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

/**
 * Somebody started a pick and gave up. The one signal Plex's own watched flag cannot give.
 *
 * "Gave up" is a real claim, and it only became a true one when `SETTLING_HOURS` landed: an outcome
 * used to be decided on percentage alone, so a film still playing, or paused an hour ago, was
 * reported here as abandoned. The server now answers `watching` for both, and this list shows only
 * what it is willing to call settled.
 */
function gaveUp(people: EngagementReport["people"]): Problem[] {
  const out: { person: string; pick: EngagementPick }[] = [];
  for (const person of people) {
    for (const pick of person.picks) {
      // `dropped` only. A `bounced` pick is under 5% — one to three minutes of a film — and at that
      // depth there is no way to tell a wrong pick from a mis-click, a scrub, or a client that
      // autoplayed. On a real 47-user server 48 of 124 unfinished picks sat under 5%, and because
      // the list was sorted ascending they were ALL you ever saw: four lines reading 1%, 3%, 3%, 5%
      // under a warning triangle, with "gave up on" — the strongest negative claim on the page —
      // attached to the least reliable data it has.
      //
      // They are still counted, in the "gave up part-way" tile. What they are not is a finding.
      if (pick.outcome === "dropped") {
        out.push({ person: personName(person), pick });
      }
    }
  }
  // MOST progress lost first. The previous order was ascending, on the reasoning that bailing at 3%
  // is worse than at 70% — true of the pick, but it selected for the noisiest rows, and the same
  // feature elsewhere refuses to call one person abandoning something a signal at all.
  out.sort((a, b) => (b.pick.percent ?? 0) - (a.pick.percent ?? 0));
  return out.slice(0, 5).map(({ person, pick }, i) => ({
    key: `gave-up-${pick.title}-${i}`,
    // Says WHEN the app is willing to make this claim, because "gave up" is the strongest negative
    // thing on the page and the rule behind it is invisible. Without it the honest reaction to
    // seeing a film you are two nights into is "the tracking is wrong", not "it will correct".
    hint: WHY_GAVE_UP_FINDING,
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

/** What the panel has to say: the problems worth listing, and whether it is too early to judge. */
function findProblems(
  report: EffectivenessReport,
  data: EngagementReport,
): { tooEarly: boolean; problems: Problem[] } {
  // A maturity gate. `landing.rate === null` is the server saying no pick has had its full N
  // days yet. Without it a five-minute-old install showed three amber warnings that nobody had
  // watched anything. The card says so itself when it holds them back (below) — the Impact
  // card that used to carry "Not enough time yet" now shows a viewing share instead.
  // `?.` because an older report — or a caller that builds `overall` by hand — may carry no
  // landing block at all. Absent is NOT the same as `null`: null is the server saying "too
  // early to judge", absent is no opinion, and only the first may suppress a warning.
  const tooEarly = report.overall.landing?.rate === null;
  const idle = tooEarly ? null : idlePeople(report.coverage);
  // Only when the line above covers EVERYONE. "3 of 3 people got picks and watched none"
  // followed by one line per row saying the same of each row is one fact stated three
  // ways. When only SOME people are idle, the per-row breakdown says which rows — which is
  // new information and the reason this list exists.
  const idleCoversEveryone =
    idle !== null &&
    report.coverage.users_idle === report.coverage.users_with_picks;
  const problems = [
    idle,
    ...(tooEarly || idleCoversEveryone
      ? []
      : deadRows(report.per_row)),
    unwatchedRequests(report.requests),
    ...gaveUp(data.people),
  ].filter((p): p is Problem => p !== null);
  return { tooEarly, problems };
}

export function NeedsALook({
  report,
  reportWindow,
}: {
  report: EffectivenessReport;
  reportWindow: ReportWindow;
}) {
  const engagement = useEngagement(reportWindow);
  // "Nothing to flag" is not a panel's worth of content: it stamped an all-clear card on every healthy
  // dashboard. While the reading loads the panel shows, so it does not pop in and shove the page down;
  // once it answers "nothing", it is gone.
  if (engagement.data) {
    const { tooEarly, problems } = findProblems(report, engagement.data);
    if (problems.length === 0 && !tooEarly) return null;
  }
  return (
    <ReportPanel
      title="Worth a look"
      // Covers BOTH halves of the list, which "Where the picks are not landing" did not. Three of the
      // four item kinds here are picks nobody started — idle people, dead rows, unfetched requests.
      // The fourth is the opposite: somebody DID start it. A partial watch is the pick landing and
      // then losing them, which is a different fact and not a failure to land.
      hint={<>Picks nobody started, and ones they started but didn&rsquo;t finish.</>}
      flush
    >
        <QueryBoundary
          query={engagement}
          skeleton={<Skeleton className="m-4 h-16 sm:mx-5" />}
        >
          {(data) => {
            const { tooEarly, problems } = findProblems(report, data);
            // The same size as every other panel's body text: a quieter note read as a footnote to a
            // list that was not there.
            const tooEarlyNote = tooEarly ? (
              <p className={cn("text-sm text-muted-foreground", panelRowClass, problems.length > 0 && "border-t")}>
                Too early to say who isn&rsquo;t watching: a pick gets{" "}
                {report.overall.landing?.matured_days ?? 30} days before it
                counts.
              </p>
            ) : null;
            if (problems.length === 0 && tooEarly) return tooEarlyNote;
            return (
              <>
                <ul className="divide-y">
                  {problems.map((problem) => (
                    <li key={problem.key} className={cn("flex items-start gap-2.5", panelRowClass)}>
                      <AlertTriangle
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary/70"
                        aria-hidden="true"
                      />
                      <div className="min-w-0 text-sm text-muted-foreground">
                        <p className="leading-snug">
                          {problem.text}
                          {problem.hint && <Why text={problem.hint} />}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
                {tooEarlyNote}
              </>
            );
          }}
        </QueryBoundary>
    </ReportPanel>
  );
}
