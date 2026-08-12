import { ChevronRight, Telescope, Users } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { UserAvatar } from "@/components/user-avatar";
import { formatDuration, runStatusLabel, runStatusVariant } from "@/lib/format";
import { groupRunByRow, rowSummary, type RunRowGroup } from "@/lib/run-rows";
import type { RunDetail } from "@/lib/types";

/** Why a person got (or did not get) this row, as a short phrase beside their name. */
const DECISION_LABEL: Record<string, string> = {
  not_due: "not due",
  muted: "muted",
  not_in_audience: "not in the audience",
};

function PersonLine({
  person,
  runId,
}: {
  person: RunRowGroup["people"][number];
  runId: number;
}) {
  const why = person.decision ? DECISION_LABEL[person.decision] : "";
  return (
    <li className="flex items-center gap-2 border-t px-3 py-2 text-sm first:border-t-0">
      <UserAvatar name={person.displayName} className="h-6 w-6 text-[10px]" />
      <span className="min-w-0 flex-1 truncate">{person.displayName}</span>
      <Badge variant={runStatusVariant(person.status)} className="shrink-0">
        {runStatusLabel(person.status)}
      </Badge>
      {why && (
        <span className="shrink-0 text-xs text-muted-foreground">{why}</span>
      )}
      {person.hasTrace && person.userId !== undefined && (
        <Button asChild variant="ghost" size="sm" className="shrink-0">
          <Link to={`/runs/${runId}/trace/${person.userId}`}>
            <Telescope aria-hidden="true" />
            Trace
          </Link>
        </Button>
      )}
    </li>
  );
}

function RowCard({ group, runId }: { group: RunRowGroup; runId: number }) {
  // Collapsed by default. A server with 46 people and three rows is otherwise one unbroken scroll —
  // the whole complaint that produced this view.
  const [open, setOpen] = useState(false);
  const shared = group.shared;
  const summary = rowSummary(group);

  return (
    <div className="rounded-lg border">
      <div className="flex flex-wrap items-center gap-2 p-3">
        {group.kind === "per_person" ? (
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
            <span className="min-w-0">
              <span className="block truncate font-medium">{group.title}</span>
              <span className="block text-xs text-muted-foreground">
                {summary}
              </span>
            </span>
          </button>
        ) : (
          <span className="min-w-0 flex-1 pl-6">
            <span className="block truncate font-medium">{group.title}</span>
            <span className="block text-xs text-muted-foreground">
              {summary}
              {shared?.duration_ms
                ? ` · ${formatDuration(shared.duration_ms)}`
                : ""}
            </span>
          </span>
        )}
        <Badge variant="outline" className="shrink-0">
          {group.kind === "shared" ? "Shared" : "Per-person"}
        </Badge>
        {shared && (
          <Badge variant={runStatusVariant(shared.status)} className="shrink-0">
            {runStatusLabel(shared.status)}
          </Badge>
        )}
        {shared?.has_trace && (
          <Button asChild variant="ghost" size="sm" className="shrink-0">
            <Link to={`/runs/${runId}/trace/row/${group.slug}`}>
              <Telescope aria-hidden="true" />
              Trace
            </Link>
          </Button>
        )}
      </div>

      {/* A shared row's own explanation. It has no person to carry it, so a skip used to be
          answerable from the container log and nowhere else (issue #3). */}
      {shared?.reason && (
        <p className="border-t px-3 py-2 text-sm text-muted-foreground">
          {shared.reason}
        </p>
      )}
      {shared?.error && (
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all border-t px-3 py-2 font-mono text-xs text-destructive-text">
          {shared.error}
        </pre>
      )}

      {open && group.people.length > 0 && (
        <ul className="border-t">
          {group.people.map((person) => (
            <PersonLine key={person.slug} person={person} runId={runId} />
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The Rows tab: what this run did, grouped by the thing it actually does — a row.
 *
 * The page used to list people, which meant a SHARED row (built once for the whole server, belonging
 * to nobody) had nowhere to appear. On a run where no per-person row was due, that produced a wall of
 * "skipped" with the run's only real output — a shared row of 40 picks — nowhere on screen.
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
  const groups = groupRunByRow(run, titles, idBySlug);
  if (groups.length === 0) {
    return (
      <div className="flex gap-3 rounded-lg border bg-muted/40 p-4 text-sm">
        <Users
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="space-y-1">
          <p className="font-medium">No rows in this run</p>
          <p className="text-muted-foreground">
            Runs from before this view existed recorded results per person
            rather than per row — the Log tab still has everything that
            happened.
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
        />
      ))}
    </div>
  );
}
