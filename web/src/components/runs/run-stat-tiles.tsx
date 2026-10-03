import {
  Clock,
  Download,
  Layers,
  Search,
  Shuffle,
  Sparkles,
  Users,
} from "lucide-react";
import { Fragment, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Link } from "react-router";

import { StatusCell, StatusRow, StatusStrip } from "@/components/status-strip";
import { Badge } from "@/components/ui/badge";
import { formatDuration, runElapsedMs, runStatusLabel } from "@/lib/format";
import {
  hasPrivacyWarning,
  privacyFindings,
  runPrivacyVerdict,
  type RunPrivacy,
} from "@/lib/run-privacy";
import { tokenSteps } from "@/lib/run-format";
import type { RunDetail } from "@/lib/types";

/** A finished run's summary: one strip of facts read at a glance, rather than one dense text line. */

/**
 * A hint's parts joined by " · ", wrapping between parts before it wraps inside one: "web search" at
 * the end of one line and "467,463" at the start of the next reads as two separate figures. Each part
 * is an inline-block, so a tile too narrow for a whole part still wraps it rather than overflowing.
 * The dot rides at the end of the part before it, so no line starts with one.
 */
function HintParts({ parts }: { parts: string[] }) {
  return parts.map((part, i) => (
    <Fragment key={part}>
      {i > 0 && " "}
      <span className="inline-block">
        {part}
        {i < parts.length - 1 && " ·"}
      </span>
    </Fragment>
  ));
}

/** What "0 requested" was arrived at from.
 *
 * A bare zero reads identically whether nothing was wanted, the floors emptied the pool, or the
 * rating gate ran out of lookups before reaching anything good — and only the last is something the
 * owner can act on. It took reading the container log by hand to tell them apart (2026-08-18).
 */
function requestHint(s: RunDetail["stats"]): string {
  const requested = s.titles_requested ?? 0;
  // Deliberately names no app: the same run can route to Radarr/Sonarr or to Overseerr, and this
  // tile has no access to which. "Sent to be downloaded" is true either way.
  if (requested > 0) return "sent to be downloaded";
  // Queued FIRST, and before any talk of the floors: a run that put five titles in the inbox worked
  // exactly as configured, and "none good enough" would send the owner hunting a rating problem that
  // does not exist. Caught on a real run whose auto_min_demand had just been raised (2026-08-18).
  const queued = s.requests_queued;
  if (queued) return `${queued} waiting for you to approve in Requests`;
  // ABSENT is not zero. A run recorded before this key existed cannot tell us whether titles are
  // waiting, so claiming "none were good enough" asserts something the data does not support — and
  // sends the reader at the rating floor when the auto-send bar may be what held them. Same trap as
  // `wanted` in the notification builder.
  if (queued === undefined) return "see Requests for anything waiting";
  const wanted = s.requests_wanted;
  // A healthy run on a complete library also lands on pool === 0, so blaming the floors there would
  // report a fault where there is none. `wanted` is what tells the two apart.
  // "new" is doing real work: `wanted` is net of titles already sent or rejected, so a run whose
  // whole inbox was actioned also lands here. Titles ARE missing; they have all been dealt with.
  if (wanted === 0) return "nothing new was missing";
  const pool = s.requests_pool ?? 0;
  if (pool === 0) return "nothing cleared the demand or year limits";
  const examined = s.requests_examined ?? 0;
  // Neither number is a count of TITLES once a run has several rows: both are sums of per-row
  // checks, so a title two rows want is counted twice — while `requests_wanted` above is distinct.
  // Printing "of 3000 wanted" beside "1000 wanted" made the two disagree on the same card, so the
  // word does not appear here at all (release review 2026-08-18).
  if (examined < pool) return `rated ${examined} of ${pool} — none good enough`;
  return `rated all ${pool} — none cleared the rating limit`;
}

export function RunStatTiles({ run }: { run: RunDetail }) {
  const s = run.stats;
  const elapsed = runElapsedMs(run.began_at, run.finished_at);
  const failed = s.users_error ?? 0;
  // Skipped is neither a success nor a failure — a run where everyone was skipped used to read
  // "3 · all succeeded" above three rows badged "Skipped".
  const skipped = s.users_skipped ?? 0;
  const requested = s.titles_requested ?? 0;
  const tokens = s.llm_tokens ?? 0;
  const exa = s.exa_searches ?? 0;
  const exaCacheHits = s.exa_cache_hits ?? 0;
  // Shared rows belong to nobody, so the people counters cannot see them — and a run whose only
  // work was a shared row therefore reported "0 · 46 skipped, built nothing" directly above the row
  // that had just placed 40 picks. Rows built is the honest headline for what a run DID.
  const sharedRows = run.shared_rows ?? [];
  const sharedBuilt = sharedRows.filter((row) => row.status === "ok").length;
  const perPersonRows = new Set<string>();
  for (const user of run.users) {
    for (const [slug, decision] of Object.entries(user.rows_considered ?? {})) {
      if (decision === "due") perPersonRows.add(slug);
    }
    for (const entry of user.breakdown ?? []) {
      if (entry.row_slug) perPersonRows.add(entry.row_slug);
    }
  }
  const rowsBuilt = perPersonRows.size + sharedBuilt;
  const rowsHint = [
    perPersonRows.size > 0 ? `${perPersonRows.size} per-person` : "",
    sharedBuilt > 0 ? `${sharedBuilt} shared` : "",
  ]
    .filter(Boolean)
    .join(", ");
  // "web search 467,463 · final picks 52,625" — which AI step the tokens went to.
  const steps = tokenSteps(s.llm_tokens_by_step);
  // In and out rather than one total when the run measured both: output is billed at several times the
  // input rate (5x on Claude Haiku), so a total alone cannot say where the money went. The steps stay
  // too, on a line of their own — the split replaced them outright once, and nobody asked for that. A
  // run recorded before the split was measured keeps the older wording.
  const output = s.llm_output_tokens;
  const tokenHint =
    output != null && output <= tokens ? (
      <>
        <HintParts
          parts={[
            `${(tokens - output).toLocaleString()} in`,
            `${output.toLocaleString()} out`,
          ]}
        />
        {steps.length > 0 && (
          <>
            {" · "}
            <HintParts parts={steps} />
          </>
        )}
      </>
    ) : steps.length > 0 ? (
      `${steps.join(" · ")} · sent + received`
    ) : (
      "sent + received"
    );
  const showTokens = tokens > 0;
  const showExa = exa > 0 || exaCacheHits > 0;
  // Only a warning or a failure earns a dot in place of the icon: these used to colour the icon, and a
  // green dot on every healthy run is a light nobody reads.
  const peopleTone =
    failed > 0
      ? "error"
      : (skipped > 0 && sharedBuilt === 0) || run.status === "error"
        ? "warn"
        : undefined;
  return (
    <StatusStrip label="Run summary">
      {/* The answer to "how did it go", in the order it is asked: did it work, how long, for whom,
          is everything still private, what changed. The privacy column is the widest because its
          value is a sentence, not a number. */}
      <StatusRow className="lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.8fr)_minmax(0,0.8fr)_minmax(0,1.65fr)_minmax(0,1fr)] lg:[&>*:last-child:nth-child(odd)]:col-span-1">
        <ResultCell run={run} />
        <StatusCell
          icon={Clock}
          label="Duration"
          value={elapsed != null ? formatDuration(elapsed) : "—"}
          sub={elapsed != null && run.began_at && run.finished_at ? (
            <>
              <span className="whitespace-nowrap">{clockTime(run.began_at)} →</span>{" "}
              <span className="whitespace-nowrap">{clockTime(run.finished_at)}</span>
            </>
          ) : (
            "start → finish"
          )}
        />
        <StatusCell
          icon={Users}
          tone={peopleTone}
          label="People"
          value={s.users_ok ?? 0}
          sub={
            failed > 0
              ? `${failed} failed${skipped > 0 ? `, ${skipped} skipped` : ""}`
              : skipped > 0
                ? // Only a WARNING when the run built nothing at all. A shared-row run skips every
                  // person by design, and flagging that amber said "something went wrong" about the
                  // normal outcome of the thing the operator asked for.
                  sharedBuilt > 0
                  ? `${skipped} skipped — no per-person row was due`
                  : `${skipped} skipped, built nothing`
                : // Everyone can succeed while the RUN fails (a refused share filter belongs to no
                  // person) — "all succeeded" under a "Failed" badge is how that looked before.
                  run.status === "error"
                  ? (s.users_ok ?? 0) > 0
                    ? "built, but not promoted"
                    : "nobody was built"
                  : "all succeeded"
          }
        />
        <PrivacyCell run={run} />
        <StatusCell
          icon={Shuffle}
          label="Titles changed"
          value={`+${s.titles_added ?? 0} / −${s.titles_removed ?? 0}`}
          sub="added / rotated out"
        />
      </StatusRow>
      {/* What the run made and spent, as ONE quiet line under the strip rather than a second grid:
          a grid of two to four cells under five left a ragged row of half-empty tiles. */}
      <div className="flex flex-wrap gap-x-6 gap-y-1.5 border-t px-4 py-3 text-[13px] text-muted-foreground">
        <MetaFact
          icon={Layers}
          label="Rows built"
          value={rowsBuilt}
          // A failed run that built nothing did not find "nothing due" — it never got that far.
          hint={rowsHint || (run.status === "error" ? "none were built" : "nothing was due")}
        />
        <MetaFact
          icon={Download}
          warn={Boolean(s.requests_warnings?.length)}
          label="Requested"
          value={requested}
          hint={s.requests_warnings?.length ? s.requests_warnings.join("; ") : requestHint(s)}
        />
        {showTokens && (
          <MetaFact
            icon={Sparkles}
            label="AI tokens"
            value={tokens.toLocaleString()}
            hint={tokenHint}
            // The owner asked whether this includes cached tokens. It is each call's input and output
            // tokens as the provider reported them: Anthropic's `input_tokens` excludes cache reads and
            // writes (and Shortlist sets no cache_control, so both are 0); OpenAI's `total_tokens` and
            // Gemini's `total_token_count` count cached input inside the prompt figure.
            title="Input and output tokens the AI provider reported for each call this run, added up — what it bills on, shown as input and output because output costs several times more. With Claude nothing is cached, so this is every token sent and received. OpenAI and Gemini count input they served from their own prompt cache in here too, and bill that part at a discount. The 7-day web-search cache saves web searches, not tokens. Turn AI sources off in Settings → Finding titles to lower it."
          />
        )}
        {showExa && (
          <MetaFact
            icon={Search}
            label="Web searches"
            value={exa}
            // A warm cache means most lookups never hit the backend — showing only the "1" that did
            // made a fully-cached run look like the source did nothing. The hint names what it served.
            hint={
              exaCacheHits > 0
                ? `searched · ${exaCacheHits.toLocaleString()} from cache`
                : "web lookups · one per recent watch"
            }
            // Vendor-neutral: the same counter serves Exa and a self-hosted SearXNG.
            title="External web-search requests this run actually made — a count, not tokens. Exa bills per request and SearXNG rate-limits per request, so it is tracked apart from token spend. Results are cached for 7 days and shared across everyone, so most lookups are served from cache and cost nothing."
          />
        )}
      </div>
    </StatusStrip>
  );
}

/** One figure in the run's quiet second line: icon (or a warning dot), what it is, the number, why. */
function MetaFact({
  icon: Icon,
  label,
  value,
  hint,
  warn = false,
  title,
}: {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  hint: ReactNode;
  warn?: boolean;
  title?: string;
}) {
  return (
    <p className="min-w-0 [overflow-wrap:anywhere]" title={title}>
      {warn ? (
        <span className="mr-1.5 inline-block h-2 w-2 rounded-full bg-warning align-middle" aria-hidden="true" />
      ) : (
        <Icon className="mr-1.5 inline h-3.5 w-3.5 align-[-2px]" aria-hidden="true" />
      )}
      <span>{label}</span> <span className="font-semibold text-foreground tabular-nums">{value}</span>
      {" · "}
      <span>{hint}</span>
    </p>
  );
}

/** "02:30:04" — a run is often seconds long, so the start → finish line carries seconds. */
function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Did the run work. "OK with warnings" is not a status of its own — the server has none (five queries
 * filter on `ok`/`error`) — it is an OK run whose privacy measurement flagged somebody.
 */
function ResultCell({ run }: { run: RunDetail }) {
  const failed = run.stats.users_error ?? 0;
  if (run.status === "ok") {
    const warnings = privacyFindings(run.privacy).length;
    const warned = hasPrivacyWarning(run);
    return (
      <StatusCell
        label="Result"
        tone={warned ? "warn" : "ok"}
        value={
          warned ? (
            <Badge variant="warning" className="border-warning/40">
              OK with warnings
            </Badge>
          ) : (
            "OK"
          )
        }
        sub={[
          failed > 0 ? `${failed} ${failed === 1 ? "person" : "people"} failed` : "No errors",
          ...(warned ? [`${warnings} ${warnings === 1 ? "warning" : "warnings"}`] : []),
        ].join(" · ")}
      />
    );
  }
  if (run.status === "error") {
    return (
      <StatusCell
        label="Result"
        tone="error"
        value="Failed"
        sub={
          run.promotion_blockers.length > 0
            ? "Nothing was promoted"
            : failed > 0
              ? `${failed} ${failed === 1 ? "person" : "people"} failed`
              : "Didn’t finish cleanly"
        }
      />
    );
  }
  return <StatusCell label="Result" tone="neutral" value={runStatusLabel(run.status)} />;
}

/** The run's own words for one flagged account, for the link under the privacy count. */
function findingPhrase(name: string, username: string, privacy: RunPrivacy): string {
  const key = username.toLowerCase();
  const listed = (names: string[] | null) => (names ?? []).some((n) => n.toLowerCase() === key);
  if (listed(privacy.can_see_others)) return `${name} can see others’ rows`;
  if (listed(privacy.unreadable_filters)) return `Plex can’t read ${name}’s restrictions`;
  return `Plex isn’t applying ${name}’s hide rules`;
}

/**
 * How many accounts hide every row that is not theirs, as far as THIS run can vouch for.
 *
 * Says "Not measured" rather than a count whenever the run did not look — an older run, a dry run, a
 * run that died before the merge — and "Not fully measured" when Plex's filter read did not run. A
 * count built on a check nobody ran is the all-clear this cell must never print.
 */
function PrivacyCell({ run }: { run: RunDetail }) {
  const verdict = runPrivacyVerdict(
    run.privacy,
    run.users.map((user) => user.username),
  );
  const displayName = (username: string) =>
    run.users.find((user) => user.username.toLowerCase() === username.toLowerCase())?.display_name ||
    username;
  const flaggedLink = (flagged: string[]) => {
    const first = flagged[0];
    if (!first || !run.privacy) return null;
    return (
      <Link
        to="/privacy"
        className="rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {findingPhrase(displayName(first), first, run.privacy)} →
      </Link>
    );
  };

  switch (verdict.kind) {
    case "not_measured":
      return (
        <StatusCell
          label="Privacy"
          tone="neutral"
          value="Not measured"
          sub={
            run.dry_run
              ? "A dry run builds no rows, so there was nothing to hide"
              : "This run didn’t check who can see whose rows"
          }
        />
      );
    case "partly_measured":
      return (
        <StatusCell
          label="Privacy"
          tone={verdict.flagged.length > 0 ? "warn" : "neutral"}
          value="Not fully measured"
          sub={flaggedLink(verdict.flagged) ?? "Plex’s share filters weren’t read on this run"}
        />
      );
    case "no_accounts":
      return <StatusCell label="Privacy" tone="neutral" value="Nothing to check" sub="No accounts were in this run" />;
    case "counted":
      return (
        <StatusCell
          label="Privacy"
          tone={verdict.hiding === verdict.total ? "ok" : "warn"}
          value={`${verdict.hiding} of ${verdict.total} accounts hide every row`}
          sub={
            flaggedLink(verdict.flagged) ??
            (verdict.enforcementChecked
              ? "Measured by this run"
              : "Hide rules stored; Plex’s enforcement wasn’t spot-checked")
          }
        />
      );
  }
}
