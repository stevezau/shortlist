import {
  AlertCircle,
  Check,
  CircleDashed,
  CircleSlash,
  Loader2,
} from "lucide-react";
import type { ReactNode } from "react";
import { Fragment, useState } from "react";

import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { UserAvatar } from "@/components/user-avatar";
import { formatDuration } from "@/lib/format";
import { personName } from "@/lib/user-names";
import { cn } from "@/lib/utils";
import type { RunRowCost, RunUserResult } from "@/lib/types";

/** A sticky section header inside the scrollable user list. */
function GroupLabel({ children }: { children: ReactNode }) {
  return (
    <p className="sticky top-0 z-10 bg-muted/90 px-3 py-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground backdrop-blur-sm">
      {children}
    </p>
  );
}

const STATUS_CLASS = "inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground";

/** The right-hand status of a person's list row: failed, pending, skipped, not built, or done (with its time). */
function UserStatus({
  result,
  cost,
  built,
}: {
  result: RunUserResult;
  cost?: RunRowCost | null;
  built?: boolean | null;
}) {
  if (result.error !== null) {
    return (
      <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-destructive-text">
        <AlertCircle className="h-3.5 w-3.5" aria-hidden="true" />
        Failed
      </span>
    );
  }
  if (result.status === "pending") {
    return (
      <span className={STATUS_CLASS}>
        Pending
        <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
      </span>
    );
  }
  if (result.status === "skipped") {
    return (
      <span className={STATUS_CLASS}>
        Skipped
        <CircleSlash className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  if (built === false) {
    // Nothing was written for them on THIS row — the run was cancelled before it got here, the
    // row was muted for them, or it produced no picks. A cost exists anyway: the row timer
    // starts before the cancel check, so this used to render a green tick beside "0s", which
    // says "built instantly" about a row that was never built at all.
    return (
      <span className={STATUS_CLASS}>
        Not built
        <CircleDashed className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    );
  }
  // This list is row-scoped — it lives inside ONE row's card — so the duration shown here is
  // THIS row's own time, not the person's whole-run total (`result.duration_ms`), which used
  // to repeat the same number beside every name regardless of which row was open.
  return (
    <span className={STATUS_CLASS}>
      {cost ? formatDuration(cost.duration_ms - cost.blocked_ms) : "Done"}
      <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
    </span>
  );
}

/** One person as a full-width list row — far more scannable at 48 users than a wall of pills:
 *  name on the left, status/duration on the right, selected row highlighted. */
function UserRow({
  result,
  selected,
  onSelect,
  cost,
  built,
  added,
  notPrivate,
}: {
  result: RunUserResult;
  selected: string;
  onSelect: (slug: string) => void;
  /** THIS row's own cost for this person. `undefined` when the caller passed no per-row costs at
   *  all (a hypothetical non-row context); `null` on a legacy run that never measured it — either
   *  way this is "not recorded", not "0s", so both fall back to a plain "Done". */
  cost?: RunRowCost | null;
  /** Did this row deliver anything to them? `null`/`undefined` = not recorded, so say nothing. */
  built?: boolean | null;
  /** Titles THIS row added for them this run; nothing is said when it is zero or unknown. */
  added?: number;
  /** This run found their account can see rows that are not theirs. */
  notPrivate?: boolean;
}) {
  const failed = result.error !== null;
  const isSelected = result.slug === selected;
  return (
    <button
      type="button"
      role="tab"
      aria-selected={isSelected}
      onClick={() => onSelect(result.slug)}
      className={cn(
        "flex w-full items-center gap-3 border-l-2 px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        failed ? "border-l-destructive/70" : "border-l-transparent",
        isSelected ? "bg-raised shadow-selected-y" : "hover:bg-muted/60",
      )}
    >
      <UserAvatar name={result.username} size="sm" />
      <span className="min-w-0 flex-1">
        <span className="block break-words font-medium">
          {personName(result)}
        </span>
        {added !== undefined && added > 0 && (
          <span className="block text-xs text-muted-foreground tabular-nums">
            +{added} new
          </span>
        )}
      </span>
      {notPrivate && (
        <Badge variant="warning" className="shrink-0 border-warning/40 px-2 py-0">
          not private
        </Badge>
      )}
      <UserStatus result={result} cost={cost} built={built} />
    </button>
  );
}

/** The user nav at the top of a run. At 48 users a flat grid is a wall, so: a one-line summary,
 *  failures always up front, the (usually many) successes tucked behind a toggle, and a search box. */
export function UserTabs({
  results,
  selected,
  onSelect,
  showSummary = true,
  costBySlug,
  builtBySlug,
  newBySlug,
  notPrivate,
}: {
  results: RunUserResult[];
  selected: string;
  onSelect: (slug: string) => void;
  /** False where the caller already states the progress — the Rows tab's card header says
   *  "10 of 46 people done" two lines above, so repeating it here in different words was noise. */
  showSummary?: boolean;
  /** THIS row's per-person cost, keyed by slug. Optional so a hypothetical future non-row caller
   *  still compiles — every real caller today is row-scoped, so every `UserRow` gets one. */
  costBySlug?: Map<string, RunRowCost | null>;
  /** Whether THIS row delivered anything to each person, keyed by slug. Separate from `costBySlug`
   *  because a cost exists for rows that were never written — see `RunRowPerson.built`. */
  builtBySlug?: Map<string, boolean | null>;
  /** Titles THIS row added for each person, keyed by slug. */
  newBySlug?: Map<string, number>;
  /** Lower-cased usernames the run's privacy measurement flagged. */
  notPrivate?: Set<string>;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | "failed" | "ok">("all");
  const q = query.trim().toLowerCase();
  const failedTotal = results.filter((r) => r.error !== null).length;
  const pendingTotal = results.filter((r) => r.status === "pending").length;
  const skippedTotal = results.filter(
    (r) => r.error === null && r.status === "skipped",
  ).length;
  const okTotal = results.length - failedTotal - skippedTotal - pendingTotal;
  const isPending = (r: RunUserResult) => r.status === "pending";
  const isSkipped = (r: RunUserResult) =>
    r.error === null && r.status === "skipped";
  const isOk = (r: RunUserResult) =>
    r.error === null && !isSkipped(r) && !isPending(r);
  const mixed = failedTotal > 0 && okTotal + skippedTotal > 0; // a filter only helps when there's a mix
  const byStatus =
    !mixed || filter === "all"
      ? results
      : results.filter((r) =>
          filter === "failed" ? r.error !== null : !r.error,
        );
  const shown = q
    ? byStatus.filter(
        (r) =>
          r.username.toLowerCase().includes(q) ||
          (r.display_name ?? "").toLowerCase().includes(q),
      )
    : byStatus;
  const failed = shown.filter((r) => r.error !== null);
  const pending = shown.filter(isPending);
  const ok = shown.filter(isOk);
  const skipped = shown.filter(isSkipped);
  const many = results.length > 10;
  const bothGroups =
    [failed.length, ok.length, skipped.length, pending.length].filter(Boolean)
      .length > 1;
  // Pending always carries its label; the others only when more than one group is showing.
  const groups = [
    { label: "Failed", results: failed, labelled: bothGroups },
    { label: "Succeeded", results: ok, labelled: bothGroups },
    { label: "Skipped", results: skipped, labelled: bothGroups },
    { label: "Pending", results: pending, labelled: true },
  ];

  return (
    <div className="space-y-3" role="tablist" aria-label="Users in this run" aria-orientation="vertical"
      onKeyDown={(event) => {
        if (!(event.target instanceof HTMLElement) || event.target.getAttribute("role") !== "tab") return;
        const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
        const index = tabs.indexOf(event.target as HTMLButtonElement);
        const next = event.key === "ArrowDown" ? (index + 1) % tabs.length : event.key === "ArrowUp" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
        if (next === null) return;
        event.preventDefault();
        tabs[next]?.focus();
        tabs[next]?.click();
      }}>

      <div className="space-y-2">
        {mixed ? (
          <Segmented<"all" | "failed" | "ok">
            value={filter}
            onChange={setFilter}
            ariaLabel="Filter people by status"
            options={[
              { value: "all", label: `All ${results.length}` },
              { value: "failed", label: `Failed ${failedTotal}` },
              { value: "ok", label: `No errors ${results.length - failedTotal}` },
            ]}
          />
        ) : (
          // Only the plain progress tally is suppressed by `showSummary`. Failures and skips are an
          // EXPLANATION, not a restatement — "3 skipped — nothing was built" answers a question the
          // card header's "10 of 46 done" does not, so it shows either way.
          (() => {
            const line =
              pendingTotal > 0 && okTotal === 0 && failedTotal === 0 ? (
                showSummary ? (
                  `${pendingTotal} waiting to start`
                ) : null
              ) : failedTotal > 0 ? (
                <span className="font-medium text-destructive-text">
                  {failedTotal} failed
                </span>
              ) : okTotal === 0 && skippedTotal > 0 ? (
                `${skippedTotal} skipped — nothing was built`
              ) : showSummary ? (
                `${okTotal} succeeded${skippedTotal > 0 ? `, ${skippedTotal} skipped` : ""}${pendingTotal > 0 ? `, ${pendingTotal} pending` : ""}`
              ) : null;
            return line === null ? null : (
              <p className="text-sm text-muted-foreground">{line}</p>
            );
          })()
        )}
        {many && (
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Find a person…"
            className="h-8 w-full rounded-md border bg-background px-2.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label="Search users in this run"
          />
        )}
      </div>

      {/* One scannable, scrollable list — failures first, so a partly-failed run opens on what you
          came for. A vertical list reads far better than a wrapped grid of 48 near-identical pills. */}
      <div>
        <div className="max-h-96 divide-y divide-border/50 overflow-y-auto">
          {groups.map(({ label, results: group, labelled }) => (
            <Fragment key={label}>
              {labelled && group.length > 0 && (
                <GroupLabel>
                  {label} · {group.length}
                </GroupLabel>
              )}
              {group.map((result) => (
                <UserRow
                  key={result.slug}
                  result={result}
                  selected={selected}
                  onSelect={onSelect}
                  cost={costBySlug?.get(result.slug)}
                  built={builtBySlug?.get(result.slug)}
                  added={newBySlug?.get(result.slug)}
                  notPrivate={notPrivate?.has(result.username.toLowerCase())}
                />
              ))}
            </Fragment>
          ))}
          {shown.length === 0 && (
            <p className="px-3 py-8 text-center text-sm text-muted-foreground">
              No one matches “{query}”.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
