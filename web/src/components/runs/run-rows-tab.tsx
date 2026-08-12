import { ChevronRight, CircleSlash, Layers, Telescope } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { PickList } from "@/components/pick-list";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { UserAvatar } from "@/components/user-avatar";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import { friendlyError } from "@/lib/run-format";
import {
  groupRunByRow,
  libraryLabel,
  rowSummary,
  type RunRowGroup,
  type RunRowPerson,
} from "@/lib/run-rows";
import type { RunDetail, RunLibraryBreakdown } from "@/lib/types";

/** Why this person got, or did not get, this row — shown beside their name. */
const DECISION_LABEL: Record<string, string> = {
  muted: "muted",
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
 * What a row produced, per library — the same panel for a person's slice of a per-person row and
 * for a shared row's single result.
 *
 * This is the consistency the view turns on: every row expands to its picks, and the only thing a
 * per-person row adds is somebody to choose first. Before this, expanding a row gave a list of
 * names with a status and nothing else, so you could see WHO ran but never what they got without
 * leaving the page.
 */
function RowPicks({
  breakdown,
  fallbackPicks,
  note,
}: {
  breakdown: RunLibraryBreakdown[];
  fallbackPicks?: { rank: number; title: string }[];
  note?: string;
}) {
  if (breakdown.length === 0) {
    // A legacy run has no per-library breakdown, but its picks are still worth showing flat.
    const picks = fallbackPicks ?? [];
    if (picks.length === 0) {
      return (
        <p className="p-4 text-sm text-muted-foreground">
          {note ?? "This row delivered nothing here."}
        </p>
      );
    }
    return (
      <div className="space-y-2 p-4">
        {note && <p className="text-sm text-muted-foreground">{note}</p>}
        <PickList picks={picks as never} collapseAfter={10} />
      </div>
    );
  }
  return (
    <div className="space-y-4 p-4">
      {note && <p className="text-sm text-muted-foreground">{note}</p>}
      {breakdown.map((entry) => (
        <div
          key={`${entry.row_slug}:${entry.library_key}`}
          className="space-y-2"
        >
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{entry.library_title}</span>
            <Badge variant="outline">{diffLabel(entry)}</Badge>
            {entry.created && <Badge variant="outline">new row</Badge>}
          </div>
          <PickList picks={entry.picks} collapseAfter={10} />
        </div>
      ))}
    </div>
  );
}

/** One person's line in a per-person row's picker. */
function PersonButton({
  person,
  selected,
  onSelect,
  runId,
}: {
  person: RunRowPerson;
  selected: boolean;
  onSelect: () => void;
  runId: number;
}) {
  const why = person.decision ? DECISION_LABEL[person.decision] : "";
  return (
    <li className="flex items-center gap-1 border-b last:border-b-0">
      <button
        type="button"
        onClick={onSelect}
        aria-current={selected ? "true" : undefined}
        className={`flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left text-sm hover:bg-muted/50 ${
          selected
            ? "bg-muted/60 font-medium shadow-[inset_2px_0_0_currentColor]"
            : ""
        }`}
      >
        <UserAvatar name={person.displayName} className="h-6 w-6 text-[10px]" />
        <span className="min-w-0 flex-1 truncate">{person.displayName}</span>
        {why && (
          <span className="shrink-0 text-xs text-muted-foreground">{why}</span>
        )}
        <Badge variant={runStatusVariant(person.status)} className="shrink-0">
          {runStatusLabel(person.status)}
        </Badge>
      </button>
      {person.hasTrace && person.userId !== undefined && (
        <Button asChild variant="ghost" size="sm" className="mr-1 shrink-0">
          <Link
            to={`/runs/${runId}/trace/${person.userId}`}
            title={`How ${person.displayName}'s picks were chosen`}
          >
            <Telescope aria-hidden="true" />
            <span className="sr-only">Trace {person.displayName}</span>
          </Link>
        </Button>
      )}
    </li>
  );
}

function RowCard({
  group,
  runId,
  defaultOpen,
}: {
  group: RunRowGroup;
  runId: number;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [picked, setPicked] = useState("");
  const shared = group.shared;
  const libraries = libraryLabel(group);

  // Default to the first FAILED person — what you opened the row to see — else the first.
  const chosen =
    group.people.find((p) => p.slug === picked) ??
    group.people.find((p) => p.status === "error") ??
    group.people[0];

  const header = (
    <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-left">
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
        {shared?.duration_ms ? ` · ${formatDuration(shared.duration_ms)}` : ""}
      </span>
    </div>
  );

  return (
    <div className="rounded-lg border">
      <div className="flex flex-wrap items-center gap-2 p-3">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <ChevronRight
            aria-hidden="true"
            className={`h-4 w-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none ${
              open ? "rotate-90" : ""
            }`}
          />
          {header}
        </button>
        {shared && (
          <Badge variant={runStatusVariant(shared.status)} className="shrink-0">
            {runStatusLabel(shared.status)}
          </Badge>
        )}
        {/* Trace sits on the thing it traces: the row itself when the row is shared, each person
            when it is per-person. Same edge either way, so there is one place to look. */}
        {shared?.has_trace && (
          <Button asChild variant="ghost" size="sm" className="shrink-0">
            <Link to={`/runs/${runId}/trace/row/${group.slug}`}>
              <Telescope aria-hidden="true" />
              Trace
            </Link>
          </Button>
        )}
      </div>

      {shared?.reason && (
        <p className="border-t px-3 py-2 text-sm text-muted-foreground">
          {shared.reason}
        </p>
      )}
      {shared?.error && (
        <pre className="max-h-40 overflow-auto border-t px-3 py-2 font-mono text-xs whitespace-pre-wrap break-all text-destructive-text">
          {shared.error}
        </pre>
      )}

      {open &&
        (group.kind === "shared" ? (
          <div className="border-t">
            <RowPicks
              breakdown={shared?.breakdown ?? []}
              fallbackPicks={shared?.picks as never}
              note={`Built once for the whole server from what several people have watched${
                libraries ? ` · ${libraries}` : ""
              }`}
            />
          </div>
        ) : (
          <div className="grid border-t md:grid-cols-[minmax(0,16rem)_1fr]">
            <ul className="max-h-96 divide-y overflow-y-auto border-b md:border-b-0 md:border-r">
              {group.people.map((person) => (
                <PersonButton
                  key={person.slug}
                  person={person}
                  selected={person.slug === chosen?.slug}
                  onSelect={() => setPicked(person.slug)}
                  runId={runId}
                />
              ))}
            </ul>
            <div className="min-w-0">
              {chosen ? (
                chosen.status === "error" ? (
                  <div role="alert" className="space-y-2 p-4">
                    <p className="text-sm font-medium">
                      {friendlyError(chosen.error ?? "")}
                    </p>
                    <pre className="max-h-40 overflow-auto rounded bg-muted/40 p-2.5 font-mono text-xs whitespace-pre-wrap break-all text-destructive-text">
                      {chosen.error}
                    </pre>
                  </div>
                ) : (
                  <RowPicks
                    breakdown={chosen.breakdown}
                    note={
                      chosen.decision && chosen.decision !== "due"
                        ? `${chosen.displayName} — ${DECISION_LABEL[chosen.decision] ?? chosen.decision}`
                        : undefined
                    }
                  />
                )
              ) : null}
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
}: {
  run: RunDetail;
  titles: Record<string, string>;
  idBySlug: Map<string, number>;
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
          runId={run.id}
          // One row is the whole story of a scoped run — make it open on arrival rather than
          // making the operator click to see the only thing that happened.
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
