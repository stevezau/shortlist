import { AlertTriangle, CheckCircle2, Info } from "lucide-react";
import { useState } from "react";

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
        out.push({ person: person.display_name || person.username, pick });
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

/**
 * The "why" behind a finding, behind an (i) rather than under it.
 *
 * These explanations were printed as a second grey line under every item, which doubled the height
 * of each and made the card's own findings harder to scan — the list is read at a glance and the
 * reasoning is consulted occasionally, so they do not deserve equal weight.
 *
 * A real `<button>`, not a `title` tooltip: a hover-only explanation does not exist on a phone, and
 * this app is read on one. Click or focus toggles it, `aria-expanded` says which, and the text lands
 * in the DOM where a screen reader can reach it rather than in an attribute it may skip.
 */
export function Why({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      {" "}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? "Hide why" : "Why?"}
        className="inline-flex translate-y-px items-center rounded-full text-muted-foreground/60 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Info className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
      {open && (
        <span className="mt-0.5 block text-xs leading-snug text-muted-foreground/70">
          {text}
        </span>
      )}
    </>
  );
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
        {/* Covers BOTH halves of the list, which "Where the picks are not landing" did not. Three of
            the four item kinds here are picks nobody started — idle people, dead rows, unfetched
            requests. The fourth is the opposite: somebody DID start it. A partial watch is the pick
            landing and then losing them, which is a different fact and not a failure to land, and
            calling it one told the owner something untrue about their own server. */}
        <p className="mt-0.5 text-xs text-muted-foreground/80">
          Picks nobody started, and ones they started but didn&rsquo;t finish.
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
              // The verdict card above totals EVERY abandonment, this list leaves out the ones under
              // 5% — so on a day whose only give-ups were bounces, a bare "everyone watched
              // something" sits directly under "N gave up part-way" and reads as one of the two
              // being wrong. Say which, rather than letting the reader work it out.
              const onlyBounces =
                report.overall.dropped === 0 && report.overall.bounced > 0;
              return (
                <p className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
                  <CheckCircle2
                    className="h-4 w-4 shrink-0 text-success"
                    aria-hidden="true"
                  />
                  {onlyBounces
                    ? report.overall.bounced === 1
                      ? `Nothing worth flagging — the one give-up above was under ${BOUNCE_FLOOR_PERCENT}% in, which is too little to read anything into. No row came up empty.`
                      : `Nothing worth flagging — the ${report.overall.bounced} give-ups above were all under ${BOUNCE_FLOOR_PERCENT}% in, which is too little to read anything into. No row came up empty.`
                    : "Everyone who got a pick watched something, and no row came up empty."}
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
                      <p className="leading-snug">
                        {problem.text}
                        {problem.hint && <Why text={problem.hint} />}
                      </p>
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
