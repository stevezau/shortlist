import { ChevronRight, CircleSlash, Layers, Telescope } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { PickList } from "@/components/pick-list";
import { Segmented } from "@/components/segmented";
import { UserPanel } from "@/components/runs/user-panel";
import { UserTabs } from "@/components/runs/user-tabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import {
  groupRunByRow,
  libraryLabel,
  rowSummary,
  type RunRowGroup,
} from "@/lib/run-rows";
import type { RunDetail, RunLibraryBreakdown, RunLogEntry } from "@/lib/types";

/** Why this person got, or did not get, this row. */
const DECISION_LABEL: Record<string, string> = {
  muted: "muted for them",
  not_in_audience: "not in the audience",
  not_due: "not due",
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
function SharedRowPanel({ group }: { group: RunRowGroup }) {
  const shared = group.shared;
  const breakdown = shared?.breakdown ?? [];
  const [active, setActive] = useState(breakdown[0]?.library_key ?? "");
  if (!shared) return null;
  const current =
    breakdown.find((entry) => entry.library_key === active) ?? breakdown[0];

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
            </div>
          )}
        </>
      )}
    </div>
  );
}

function RowCard({
  group,
  run,
  liveLog,
  defaultOpen,
}: {
  group: RunRowGroup;
  run: RunDetail;
  liveLog?: RunLogEntry[];
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [picked, setPicked] = useState("");
  const shared = group.shared;
  const libraries = libraryLabel(group);

  const results = group.people.map((person) => person.result);
  // Default to the first FAILED person — what you opened the row to see — else the first.
  const chosen =
    results.find((r) => r.slug === picked) ??
    results.find((r) => r.error !== null) ??
    results[0];
  const decision = group.people.find(
    (person) => person.result.slug === chosen?.slug,
  )?.decision;

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
              <span className="font-medium">{group.title}</span>
              {libraries && (
                <span className="text-xs tracking-wide text-muted-foreground uppercase">
                  {libraries}
                </span>
              )}
            </span>
            <span className="text-xs text-muted-foreground">
              {group.kind === "shared" ? "Shared" : "Per-person"} ·{" "}
              {rowSummary(group)}
              {shared?.duration_ms
                ? ` · ${formatDuration(shared.duration_ms)}`
                : ""}
            </span>
          </span>
        </button>
        {shared && (
          <Badge variant={runStatusVariant(shared.status)} className="shrink-0">
            {runStatusLabel(shared.status)}
          </Badge>
        )}
        {/* Trace sits on the thing it traces: the row when the row is shared, the person otherwise
            (each person's panel carries their own "How we picked"). */}
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
            <SharedRowPanel group={group} />
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
            />
            <div className="min-w-0">
              {decision && decision !== "due" && (
                <p className="mb-3 text-sm text-muted-foreground">
                  This row was {DECISION_LABEL[decision] ?? decision} on this
                  run.
                </p>
              )}
              {chosen && (
                <UserPanel run={run} result={chosen} liveLog={liveLog} />
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
}: {
  run: RunDetail;
  titles: Record<string, string>;
  idBySlug: Map<string, number>;
  liveLog?: RunLogEntry[];
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
          <p className="font-medium">This run built no rows</p>
          <p className="text-muted-foreground">
            {notInRun.length > 0
              ? `Nothing was due to rebuild. ${notInRun.length} row${notInRun.length === 1 ? " was" : "s were"} considered and skipped.`
              : "Runs from before this view existed recorded their results per person rather than per row — the Log tab still has everything that happened."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {groups.map((group) => (
        <RowCard
          key={`${group.kind}:${group.slug}`}
          group={group}
          run={run}
          liveLog={liveLog}
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
            <span>— {notInRun.map((row) => row.title).join(", ")}</span>
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
