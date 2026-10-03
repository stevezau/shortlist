import {
  AlertCircle,
  ChevronRight,
  CircleSlash,
  Layers,
  Telescope,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { PickList } from "@/components/pick-list";
import { RowName } from "@/components/rows/row-name";
import { Segmented } from "@/components/segmented";
import { UserPanel } from "@/components/runs/user-panel";
import { UserTabs } from "@/components/runs/user-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorBucket, friendlyError } from "@/lib/run-format";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import {
  groupRunByRow,
  libraryLabel,
  rowSummary,
  rowTimeMs,
  type RunRowGroup,
  type RunRowPerson,
} from "@/lib/run-rows";
import type {
  RunDetail,
  RunLibraryBreakdown,
  RunLogEntry,
  RunRowCost,
} from "@/lib/types";

const NOBODY = new Set<string>();

/** Why this person got, or did not get, this row. */
const DECISION_LABEL: Record<string, string> = {
  muted: "muted for them",
  not_in_audience: "not in the audience",
  not_due: "not due",
  out_of_season: "out of season",
};

/** "+7 −7 · kept 8" for one library's delivery. */
function diffLabel(entry: RunLibraryBreakdown): string {
  const parts = [`+${entry.added.length} −${entry.removed.length}`];
  if (entry.kept.length) parts.push(`kept ${entry.kept.length}`);
  return parts.join(" · ");
}

/**
 * A shared row's result — the same shape a person's panel uses, minus the person.
 *
 * Libraries are TABS, not stacked sections. A shared row targeting two libraries is 40 picks, and
 * printing them one after the other made the card scroll for pages; the per-person panel has always
 * shown one library at a time, so stacking here was both longer and inconsistent with it.
 */
function SharedRowPanel({
  group,
  running,
}: {
  group: RunRowGroup;
  running: boolean;
}) {
  const shared = group.shared;
  const breakdown = shared?.breakdown ?? [];
  const [active, setActive] = useState(breakdown[0]?.library_key ?? "");
  if (!shared) {
    // Nothing reported yet. Returning null left an empty box under an expanded row, which reads as
    // broken rather than as not-started — and a shared row builds LAST, after every person, so this
    // is the state it sits in for most of a run.
    return (
      <p className="p-5 text-sm text-muted-foreground">
        {running
          ? "This row builds once everyone’s own rows are done — it is pooled from what they have all watched, so it needs their viewing first."
          : "This row didn’t build in this run."}
      </p>
    );
  }
  const current =
    breakdown.find((entry) => entry.library_key === active) ?? breakdown[0];
  // Absent on runs recorded before the field existed.
  const duplicates = current?.duplicates_removed ?? [];

  return (
    <div className="space-y-4 p-5">
      <p className="text-sm text-muted-foreground">
        Built once for the whole server from what several people have watched —
        most watched first.
      </p>
      {breakdown.length === 0 ? (
        shared.picks.length > 0 ? (
          <PickList picks={shared.picks} collapseAfter={10} />
        ) : (
          <p className="text-sm text-muted-foreground">
            This row delivered nothing.
          </p>
        )
      ) : (
        <>
          {breakdown.length > 1 && (
            <Segmented
              value={current?.library_key ?? ""}
              onChange={setActive}
              ariaLabel="Libraries in this row"
              options={breakdown.map((entry) => ({
                value: entry.library_key,
                label: `${entry.library_title} · ${entry.picks.length}`,
              }))}
            />
          )}
          {current && (
            <div className="space-y-2.5">
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span className="font-medium">{current.library_title}</span>
                <Badge variant="outline">{diffLabel(current)}</Badge>
                {current.created && <Badge variant="outline">new row</Badge>}
              </div>
              <PickList picks={current.picks} collapseAfter={10} />
              {duplicates.length > 0 && (
                <p className="text-xs text-muted-foreground">
                  {duplicates.length === 1
                    ? "Removed a duplicate copy of this row"
                    : `Removed ${duplicates.length} duplicate copies of this row`}
                  : {duplicates.join(", ")}
                </p>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * "N people failed with the same problem" — one banner instead of the same error read off N rows.
 *
 * Lifted from the People tab when that went: it is about the RUN, not about any one row, so it sits
 * above them all. Losing it would have made a server-wide outage look like N unrelated failures.
 */
function CommonFailure({ run }: { run: RunDetail }) {
  const buckets = new Map<string, { count: number; msg: string }>();
  for (const user of run.users) {
    if (!user.error) continue;
    const bucket = errorBucket(user.error);
    if (!bucket) continue;
    buckets.set(bucket, {
      count: (buckets.get(bucket)?.count ?? 0) + 1,
      msg: friendlyError(user.error),
    });
  }
  const top = [...buckets.values()].sort((a, b) => b.count - a.count)[0];
  if (!top || top.count < 2) return null;
  return (
    <div
      role="alert"
      className="flex gap-3 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
      <p>
        <span className="font-medium">
          {top.count} people failed with the same problem.
        </span>{" "}
        {top.msg} Open any row below and pick them out for the raw details.
      </p>
    </div>
  );
}

function RowCard({
  group,
  run,
  liveLog,
  defaultOpen,
  idBySlug,
  focusUser,
  notPrivate,
}: {
  group: RunRowGroup;
  run: RunDetail;
  liveLog?: RunLogEntry[];
  defaultOpen: boolean;
  focusUser?: string | null;
  /** person slug -> user id, so their panel can link to their own trace. */
  idBySlug: Map<string, number>;
  /** Lower-cased usernames this run found could see rows that are not theirs. */
  notPrivate: Set<string>;
}) {
  const inThisRow = focusUser
    ? group.people.some((person) => person.result.slug === focusUser)
    : false;
  const [open, setOpen] = useState(defaultOpen || inThisRow);
  const [picked, setPicked] = useState(inThisRow ? (focusUser ?? "") : "");
  const shared = group.shared;
  const libraries = libraryLabel(group);
  const time = rowTimeMs(group);
  // Nothing has reported for this row yet — a queued run, or one still on an earlier row. Saying
  // "0 of 46 built" there reads as a failure rather than as not-started-yet.
  const notStarted = group.people.length === 0 && !shared && !run.finished_at;

  const results = group.people.map((person) => person.result);
  // Default to the first FAILED person — what you opened the row to see — else the first.
  const chosen =
    results.find((r) => r.slug === picked) ??
    results.find((r) => r.error !== null) ??
    results[0];
  // One walk over `group.people` for both: who is selected, and every person's own cost for THIS
  // row — `UserTabs`' person list is row-scoped (it only ever renders inside a row's card), so it
  // needs each person's row-specific time rather than their whole-run total.
  let chosenPerson: RunRowPerson | undefined;
  const costBySlug = new Map<string, RunRowCost | null>();
  const builtBySlug = new Map<string, boolean | null>();
  // `result.breakdown` is already narrowed to THIS row, so this is what the row added for them.
  const newBySlug = new Map<string, number>();
  let notPrivateHere = 0;
  for (const person of group.people) {
    costBySlug.set(person.result.slug, person.cost);
    builtBySlug.set(person.result.slug, person.built);
    newBySlug.set(
      person.result.slug,
      person.result.breakdown.reduce((n, entry) => n + entry.added.length, 0),
    );
    if (notPrivate.has(person.result.username.toLowerCase())) notPrivateHere += 1;
    if (person.result.slug === chosen?.slug) chosenPerson = person;
  }
  const decision = chosenPerson?.decision;

  return (
    <div className="rounded-lg border">
      <div className="flex flex-wrap items-center gap-2 p-4">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
        >
          <ChevronRight
            aria-hidden="true"
            className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none ${
              open ? "rotate-90" : ""
            }`}
          />
          <span className="flex min-w-0 flex-1 flex-col gap-0.5">
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              {/* The header spans every library the row built, so no one library can fill
                  `{library_name}`; the chip says what it is instead of dropping it. */}
              <RowName name={group.template} />
              {libraries && (
                <span className="text-xs tracking-wide text-muted-foreground uppercase">
                  {libraries}
                </span>
              )}
            </span>
            <span className="text-xs text-muted-foreground">
              {group.kind === "shared" ? "Shared" : "Per-person"} ·{" "}
              {notStarted ? "waiting to build" : rowSummary(group)}
              {notPrivateHere > 0 && (
                <span className="text-warning">{` · ${notPrivateHere} not private`}</span>
              )}
              {time !== null && (
                <span
                  title={
                    shared
                      ? "How long this shared row took to build."
                      : "Every person's time on this row, added up. People are built a few at a time, so this can be longer than the run itself. Time spent waiting for someone else's Plex write is not counted."
                  }
                >
                  {` · ${formatDuration(time)}${shared ? "" : " in total"}`}
                </span>
              )}
            </span>
          </span>
        </button>
        {shared ? (
          <Badge variant={runStatusVariant(shared.status)} className="shrink-0">
            {runStatusLabel(shared.status)}
          </Badge>
        ) : (
          notStarted && (
            <Badge variant="outline" className="shrink-0">
              Pending
            </Badge>
          )
        )}
        {/* Trace sits on the thing it traces: the row when the row is SHARED (one build for the whole
            server), and the PERSON otherwise — `UserPanel` renders their own "How we picked" button.
            This comment used to claim the latter while nothing rendered it: the per-person button had
            gone with the People tab, so a per-person trace was unreachable from the whole app. */}
        {shared?.has_trace && (
          <Button asChild variant="ghost" size="sm" className="shrink-0">
            <Link to={`/runs/${run.id}/trace/row/${group.slug}`}>
              <Telescope aria-hidden="true" />
              Trace
            </Link>
          </Button>
        )}
      </div>

      {shared?.reason && (
        <p className="border-t px-4 py-3 text-sm text-muted-foreground">
          {shared.reason}
        </p>
      )}
      {shared?.error && (
        <pre className="max-h-40 overflow-auto border-t px-4 py-3 font-mono text-xs whitespace-pre-wrap break-all text-destructive-text">
          {shared.error}
        </pre>
      )}

      {open &&
        (group.kind === "shared" ? (
          <div className="border-t">
            <SharedRowPanel group={group} running={!run.finished_at} />
          </div>
        ) : (
          // The People tab's own two components, scoped to this row: the searchable, status-grouped
          // person list, and the formatted panel with its libraries, diff legend and "How we picked".
          // Reusing them is what keeps the two tabs one design rather than two.
          <div className="grid gap-4 border-t p-4 lg:grid-cols-[minmax(0,20rem)_1fr]">
            <UserTabs
              results={results}
              selected={chosen?.slug ?? ""}
              onSelect={setPicked}
              // The card header two lines above already says "10 of 46 people done".
              showSummary={false}
              costBySlug={costBySlug}
              builtBySlug={builtBySlug}
              newBySlug={newBySlug}
              notPrivate={notPrivate}
            />
            <div className="min-w-0">
              {decision && decision !== "due" && (
                <p className="mb-3 text-sm text-muted-foreground">
                  This row was {DECISION_LABEL[decision] ?? decision} on this
                  run.
                </p>
              )}
              {chosen && (
                <UserPanel
                  run={run}
                  result={chosen}
                  liveLog={liveLog}
                  userId={idBySlug.get(chosen.slug) ?? null}
                  cost={chosenPerson?.cost ?? null}
                  setup={chosenPerson?.setup ?? null}
                />
              )}
            </div>
          </div>
        ))}
    </div>
  );
}

/**
 * The Rows tab: what this run did, grouped by the thing a run actually builds — a row.
 *
 * Scoped to the rows the run RAN. Listing every row that merely exists made a scoped run ("rebuild
 * just this row") look like it had touched rows the operator never selected — on one real run that
 * was two-thirds of the page.
 */
export function RunRowsTab({
  run,
  titles,
  idBySlug,
  liveLog,
  focusUser,
  notPrivate = NOBODY,
}: {
  run: RunDetail;
  titles: Record<string, string>;
  idBySlug: Map<string, number>;
  liveLog?: RunLogEntry[];
  /** Person slug from `?user=` — their row opens with them selected, so a link from their own page
   *  lands on their result rather than the top of a run with forty others in it. */
  focusUser?: string | null;
  /** Lower-cased usernames this run's privacy measurement flagged; empty when it measured nothing. */
  notPrivate?: Set<string>;
}) {
  const { groups, notInRun } = groupRunByRow(run, titles, idBySlug);
  const [showSkipped, setShowSkipped] = useState(false);

  if (groups.length === 0) {
    return (
      <div className="flex gap-3 rounded-lg border bg-muted/40 p-4 text-sm">
        <CircleSlash
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="space-y-1">
          {/* A RUNNING run has nothing persisted yet, so it lands here — and blaming a legacy run for
              a run that started seconds ago is a confidently wrong explanation, the exact failure
              this view exists to end. So does a run that FAILED before it knew its rows (Plex
              unreachable, issue #139). Four cases, not one. */}
          <p className="font-medium">
            {!run.finished_at
              ? "Getting ready…"
              : run.error
                ? "This run stopped before it built any rows"
                : "This run built no rows"}
          </p>
          <p className="text-muted-foreground">
            {!run.finished_at
              ? "This run hasn’t picked up its rows yet. They appear here the moment it does — the Log tab has the live detail."
              : run.error
                ? "The error above says why. The Log tab has anything the run recorded before it stopped."
                : notInRun.length > 0
                  ? `Nothing was due to run. ${notInRun.length} row${notInRun.length === 1 ? " was" : "s were"} considered and skipped.`
                  : "Runs from before this view existed recorded their results per person rather than per row — the Log tab still has everything that happened."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <CommonFailure run={run} />
      {groups.map((group) => (
        <RowCard
          key={`${group.kind}:${group.slug}:${focusUser ?? ""}`}
          group={group}
          run={run}
          liveLog={liveLog}
          idBySlug={idBySlug}
          focusUser={focusUser}
          notPrivate={notPrivate}
          // One row is the whole story of a scoped run — open it on arrival rather than making the
          // operator click to see the only thing that happened.
          defaultOpen={groups.length === 1}
        />
      ))}

      {/* Rows that exist but had nothing to do with this run. One quiet line, not a card each. */}
      {notInRun.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-1 text-xs text-muted-foreground">
          <Layers aria-hidden="true" className="h-3.5 w-3.5" />
          <span>
            {notInRun.length === 1
              ? "1 row wasn’t in this run"
              : `${notInRun.length} rows weren’t in this run`}
          </span>
          {showSkipped ? (
            <span>
              —{" "}
              {notInRun
                .map((row) =>
                  row.outOfSeason ? `${row.title} (out of season)` : row.title,
                )
                .join(", ")}
            </span>
          ) : (
            <button
              type="button"
              className="underline underline-offset-2 hover:text-foreground"
              onClick={() => setShowSkipped(true)}
            >
              Show
            </button>
          )}
        </div>
      )}
    </div>
  );
}
