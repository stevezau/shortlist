import { Check, CircleSlash, Clock, Copy, Telescope } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { PickList } from "@/components/pick-list";
import { Segmented } from "@/components/segmented";
import { Button } from "@/components/ui/button";
import { provenanceLabel } from "@/lib/pick-provenance";
import { titleLinks } from "@/lib/title-links";
import {
  friendlyError,
  rankClass,
  tokenStepBreakdown,
  webSearchSummary,
} from "@/lib/run-format";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { githubIssueSnippet } from "@/lib/github";
import { STAGE_LABELS } from "@/lib/run-stages";
import { useCopy } from "@/lib/use-copy";
import { cn } from "@/lib/utils";
import type {
  Pick,
  RunDetail,
  RunLibraryBreakdown,
  RunLogEntry,
  RunPoolCost,
  RunRowCost,
  RunUserResult,
} from "@/lib/types";

function CopyForGitHubButton({
  run,
  result,
}: {
  run: RunDetail;
  result: RunUserResult;
}) {
  const { state, copy } = useCopy();

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => copy(githubIssueSnippet(run, result))}
    >
      {state === "copied" ? (
        <Check aria-hidden="true" />
      ) : (
        <Copy aria-hidden="true" />
      )}
      {state === "copied"
        ? "Copied"
        : state === "error"
          ? "Couldn’t copy — try again"
          : "Copy for GitHub issue"}
    </Button>
  );
}

/** The score recorded for a pick when it was chosen, e.g. "TMDB 7.4".
 *
 *  Always TMDB: `Candidate.rating` is TMDB's `vote_average`, and that is what is stamped onto the
 *  pick. A server set to rank by IMDb/Trakt/Rotten Tomatoes fetches those through MDBList only to
 *  ORDER a rating-sorted row — the number is never written back — so labelling this with the
 *  configured source would put a name on a figure that did not come from it.
 *
 *  0 means "unrated at pick time", which is not a score and must not render as "TMDB 0.0". */
function ratingLabel(pick: Pick): string {
  return pick.rating ? `TMDB ${pick.rating.toFixed(1)}` : "";
}

/** One ranked pick: rank, a status dot (green = new this run), title + reason, and where it
 *  came from. */
function PickLine({ pick, isNew }: { pick: Pick; isNew: boolean }) {
  const links = titleLinks(pick);
  return (
    <li className="flex items-baseline gap-3 py-1.5">
      <span
        className={cn(
          "w-9 shrink-0 text-right text-sm font-semibold tabular-nums",
          rankClass(pick.rank),
        )}
      >
        #{pick.rank}
      </span>
      <span
        className={cn(
          "mt-1.5 h-2 w-2 shrink-0 rounded-full",
          isNew ? "bg-success" : "bg-muted-foreground/30",
        )}
        aria-label={isNew ? "new this run" : "kept"}
        title={isNew ? "New this run" : "Kept from last run"}
      />
      <span className="min-w-0 flex-1 text-sm">
        <span className="block truncate">
          <span className="font-medium">{pick.title}</span>
          {/* Release year sits with the TITLE, not on the metadata line: "is this an old film?" is
              asked while reading the name, and the Recent releases setting is judged on it. Absent
              on a cold-start pick, which comes from the library rather than a TMDB candidate. */}
          {pick.year != null && (
            <span className="text-muted-foreground tabular-nums">
              {" "}
              ({pick.year})
            </span>
          )}
          {pick.reason && (
            <span className="text-muted-foreground"> — {pick.reason}</span>
          )}
        </span>
        {/* Where it came from. This page has its own pick renderer rather than using PickList, so
            the provenance line has to be repeated here — it is the page people open to ask exactly
            this question. */}
        {(ratingLabel(pick) || provenanceLabel(pick) || links.length > 0) && (
          <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground/80">
            <span className="truncate">
              {[ratingLabel(pick), provenanceLabel(pick)]
                .filter(Boolean)
                .join(" · ")}
            </span>
            {links.map((link) => (
              <a
                key={link.label}
                href={link.href}
                target="_blank"
                rel="noopener noreferrer"
                className="hover:text-foreground hover:underline focus-visible:text-foreground focus-visible:underline"
              >
                {link.label}
              </a>
            ))}
          </span>
        )}
      </span>
    </li>
  );
}

/** One library's ranked picks: first five, a show-all toggle, and a quiet "removed" footer. */
function LibraryPicks({ entry }: { entry: RunLibraryBreakdown }) {
  const [expanded, setExpanded] = useState(false);
  const added = new Set(entry.added);
  const shown = expanded ? entry.picks : entry.picks.slice(0, 5);
  return (
    <div className="space-y-2">
      {entry.picks.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No picks in this library.
        </p>
      ) : (
        <ul className="divide-y divide-border/40">
          {shown.map((pick) => (
            <PickLine
              key={pick.rank}
              pick={pick}
              isNew={added.has(pick.title)}
            />
          ))}
        </ul>
      )}
      {entry.picks.length > 5 && (
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show fewer" : `Show all ${entry.picks.length}`}
        </Button>
      )}
      {entry.removed.length > 0 && (
        <p className="pt-1 text-xs text-muted-foreground">
          <span className="font-medium text-foreground/70">
            −{entry.removed.length} rotated out
          </span>{" "}
          — the row keeps its size, so these made room for the new picks above:{" "}
          <span className="line-through">{entry.removed.join(", ")}</span>
        </p>
      )}
      {entry.deleted.length > 0 && (
        <p className="text-xs font-medium text-destructive-text">
          Row deleted (this person no longer gets this row):{" "}
          {entry.deleted.join(", ")}
        </p>
      )}
    </div>
  );
}

/** One row (its libraries as tabs when there's more than one), showing the selected library's picks. */
function RowSection({ entries }: { entries: RunLibraryBreakdown[] }) {
  const [libKey, setLibKey] = useState(entries[0]?.library_key ?? "");
  const active =
    entries.find((entry) => entry.library_key === libKey) ?? entries[0];
  // Title and new-count follow the SELECTED library, so a `{library_name}` row title renders for
  // the tab you're viewing (e.g. "Movies Picked for You" ↔ "TV Shows Picked for You") instead of
  // being stuck on the first library's rendering.
  const added = active?.added.length ?? 0;
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-semibold">{active?.row_title}</h3>
        {added > 0 && (
          <span className="text-xs text-success">+{added} new</span>
        )}
      </div>
      {entries.length > 1 && (
        <Segmented
          value={libKey}
          onChange={setLibKey}
          ariaLabel="Library"
          options={entries.map((entry) => ({
            value: entry.library_key,
            label: `${entry.library_title} · ${entry.picks.length}`,
          }))}
        />
      )}
      {active && <LibraryPicks entry={active} />}
    </div>
  );
}

/** A key for the run results — what the dots and the strikethrough mean — so the view reads without
 *  hovering to guess. Shown once above a person's rows. */
function ResultsLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
      <span className="font-medium text-foreground/70">What changed:</span>
      <span className="inline-flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-success" aria-hidden="true" />
        New this run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span
          className="h-2 w-2 rounded-full bg-muted-foreground/30"
          aria-hidden="true"
        />
        Kept from last run
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="line-through">Title</span>
        Rotated out for variety
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span className="font-semibold tabular-nums text-amber-400">#1–3</span>
        Top picks
      </span>
    </div>
  );
}

/** The selected user's result: an error, or their rows grouped from the per-(row, library) breakdown. */
/**
 * One person's result, with their trace button.
 *
 * The button lives HERE and not on the tab that renders the panel. It used to sit in the People
 * tab's own header; when that tab was removed the Rows tab inherited the panel but not the button,
 * and a per-person trace became unreachable from anywhere in the app — while a comment in the Rows
 * tab claimed the panel carried it. Owning it here is what makes that true.
 */
export function UserPanel({
  run,
  result,
  liveLog,
  userId,
  cost,
  setup,
}: {
  run: RunDetail;
  result: RunUserResult;
  liveLog?: RunLogEntry[];
  /** This person's user id, for the trace link. Null when the run predates the user being known. */
  userId?: number | null;
  /** THIS row's cost when the panel is rendered inside a row. Omitted on a whole-person view. */
  cost?: RunRowCost | null;
  setup?: { setup_ms: number; pools: RunPoolCost[] } | null;
}) {
  // The per-step split ("gather 900, curate 2.1k") came with the header from the People tab. It was
  // that tab's only consumer, so dropping it here would have retired the breakdown from the whole app.
  const steps = tokenStepBreakdown(result.llm_tokens_by_step);
  const tokens =
    result.llm_tokens > 0
      ? ` · ${result.llm_tokens.toLocaleString()} AI tokens${steps ? ` (${steps})` : ""}`
      : "";
  return (
    <div className="space-y-3">
      {/* WHOSE result this is, and what it cost — the header the People tab had. Hoisting only the
          trace button out of that tab left it floating above the picks with nothing to belong to,
          and left the panel never naming the person whose row you were reading. */}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b pb-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="truncate font-medium">
            {userId !== null && userId !== undefined ? (
              <Link
                to={`/users/${userId}`}
                className="rounded-sm hover:text-primary hover:underline"
              >
                {result.display_name || result.username}
              </Link>
            ) : (
              result.display_name || result.username
            )}
          </span>
          <Badge
            variant={
              result.error !== null
                ? "destructive"
                : runStatusVariant(result.status)
            }
          >
            {runStatusLabel(result.status)}
          </Badge>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          {cost !== undefined ? (
            cost === null ? (
              <p className="text-right text-sm text-muted-foreground">
                Timing not recorded for this run
              </p>
            ) : (
              <p className="text-right text-sm text-muted-foreground">
                {formatDuration(cost.duration_ms - cost.blocked_ms)}
                {cost.blocked_ms >= cost.duration_ms * 0.1 &&
                  ` · ${formatDuration(cost.blocked_ms)} waiting`}
                {setup && setup.setup_ms > 0 && (
                  <>
                    {" · shared setup "}
                    {formatDuration(setup.setup_ms)}
                    {setup.pools.length > 0 &&
                      ` · ${setup.pools
                        .reduce((n, p) => n + p.tokens, 0)
                        .toLocaleString()} AI tokens`}
                    {setup.pools.some((p) => p.rows.length > 1) &&
                      ` (one pool, shared by ${setup.pools.find((p) => p.rows.length > 1)!.rows.length} rows)`}
                  </>
                )}
              </p>
            )
          ) : (
            (result.duration_ms ?? 0) > 0 && (
              <p className="text-right text-sm text-muted-foreground">
                {formatDuration(result.duration_ms)}
                {tokens}
                {webSearchSummary(result.exa_searches)}
              </p>
            )
          )}
          {result.has_trace && userId !== null && userId !== undefined && (
            <Button
              asChild
              variant="secondary"
              size="sm"
              className="shrink-0 gap-1.5 border border-primary/30 bg-primary/10 text-primary hover:bg-primary/20"
            >
              <Link to={`/runs/${run.id}/trace/${userId}`}>
                <Telescope className="h-3.5 w-3.5" aria-hidden="true" />
                How we picked
              </Link>
            </Button>
          )}
        </div>
      </div>
      <UserPanelBody run={run} result={result} liveLog={liveLog} />
    </div>
  );
}

function UserPanelBody({
  run,
  result,
  liveLog,
}: {
  run: RunDetail;
  result: RunUserResult;
  liveLog?: RunLogEntry[];
}) {
  if (result.error !== null) {
    return (
      <div role="alert" className="space-y-3 rounded-md bg-destructive/10 p-3">
        <p className="text-sm font-medium text-foreground">
          {friendlyError(result.error)}
        </p>
        {/* Raw detail is contained: it scrolls inside its own box and wraps long tokens (the encoded
            Plex uri) so it can never push the page sideways. */}
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all rounded bg-background/60 p-2.5 font-mono text-xs text-destructive-text">
          {result.error}
        </pre>
        <CopyForGitHubButton run={run} result={result} />
      </div>
    );
  }
  // A skip is a configuration outcome, not a failure — so it explains itself rather than sitting
  // on "Working on this person…" forever, which is how it read to the beta user who filed issue #3.
  if (result.status === "skipped") {
    return (
      <div className="flex gap-3 rounded-md bg-muted/40 p-3 text-sm">
        <CircleSlash
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div>
          <p className="font-medium text-foreground">
            Nothing to build for this person
          </p>
          <p className="mt-1 text-muted-foreground">
            {result.reason ??
              "No row was due for them in this run. Check that a per-person row is enabled and that they’re in its audience."}
          </p>
        </div>
      </div>
    );
  }
  if (result.breakdown.length === 0) {
    // Still running (this user hasn’t finished) or a legacy run with no breakdown.
    if (result.picks.length > 0)
      return <PickList picks={result.picks} className="mt-1" />;
    // A cold-start person whose rows are set to skip has no picks and no breakdown, but something
    // DID happen — their row was not built, and any earlier one was removed. Saying "no changes"
    // there is wrong on the one screen whose job is "what changed at 03:31".
    if (result.status === "cold_start" && result.reason) {
      return <p className="text-sm text-muted-foreground">{result.reason}</p>;
    }
    if (result.status === "ok" || result.status === "cold_start") {
      return (
        <p className="text-sm text-muted-foreground">
          No changes — this person’s rows were already up to date.
        </p>
      );
    }
    // Not started. It used to fall through to the live-log stage below and render a bare "queued…",
    // which reads like a fragment of a log rather than an answer — and it is the state most of the
    // roster is in for most of a run, so it is the panel people see most.
    // "pending" is not only "not started": a person mid-build carries it too, because a `run_users`
    // row is written only when they FINISH. So this branch has to defer to the live log the moment
    // there is one, or the panel tells you nothing on their Plex has changed while the engine is
    // writing their collection — a false claim on the screen whose job is "what changed at 03:31".
    const started = liveLog?.some((e) => e.user === result.slug) ?? false;
    if (result.status === "pending" && !started) {
      return (
        <div className="flex gap-3 rounded-md bg-muted/40 p-3 text-sm">
          <Clock
            className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <div>
            <p className="font-medium text-foreground">Waiting their turn</p>
            <p className="mt-1 text-muted-foreground">
              A run builds people a few at a time. Nothing on their Plex has
              changed yet — their rows appear here as soon as they are built.
            </p>
          </div>
        </div>
      );
    }
    // Show the latest stage from the live log for this user.
    const userLog = liveLog?.filter((e) => e.user === result.slug);
    const latest = userLog?.at(-1);
    const stageLabel = latest
      ? (STAGE_LABELS[latest.stage] ?? latest.stage)
      : null;
    const rowName = latest?.counts?.row as string | undefined;
    return (
      <p className="text-sm text-muted-foreground">
        {stageLabel
          ? `${stageLabel}${rowName ? ` — ${rowName}` : ""}…`
          : "Working on this person…"}
      </p>
    );
  }
  const rows = new Map<string, RunLibraryBreakdown[]>();
  for (const entry of result.breakdown) {
    rows.set(entry.row_slug, [...(rows.get(entry.row_slug) ?? []), entry]);
  }
  return (
    <div className="space-y-6">
      <ResultsLegend />
      {[...rows.values()].map((entries) => (
        <RowSection key={entries[0]?.row_slug} entries={entries} />
      ))}
    </div>
  );
}
