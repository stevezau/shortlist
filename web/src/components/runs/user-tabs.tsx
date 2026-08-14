import {
  AlertCircle,
  Check,
  CircleDashed,
  CircleSlash,
  Loader2,
} from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";

import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { formatDuration } from "@/lib/format";
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

/** One person as a full-width list row — far more scannable at 48 users than a wall of pills:
 *  name on the left, status/duration on the right, selected row highlighted. */
function UserRow({
  result,
  selected,
  onSelect,
  cost,
  built,
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
        isSelected ? "bg-primary/10" : "hover:bg-muted/60",
      )}
    >
      <UserAvatar name={result.username} size="sm" />
      <span className="min-w-0 flex-1 truncate font-medium">
        {result.display_name || result.username}
      </span>
      {failed ? (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-destructive-text">
          <AlertCircle className="h-3.5 w-3.5" aria-hidden="true" />
          Failed
        </span>
      ) : result.status === "pending" ? (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          Pending
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
        </span>
      ) : result.status === "skipped" ? (
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          Skipped
          <CircleSlash className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
      ) : built === false ? (
        // Nothing was written for them on THIS row — the run was cancelled before it got here, the
        // row was muted for them, or it produced no picks. A cost exists anyway: the row timer
        // starts before the cancel check, so this used to render a green tick beside "0s", which
        // says "built instantly" about a row that was never built at all.
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          Not built
          <CircleDashed className="h-3.5 w-3.5" aria-hidden="true" />
        </span>
      ) : (
        // This list is row-scoped — it lives inside ONE row's card — so the duration shown here is
        // THIS row's own time, not the person's whole-run total (`result.duration_ms`), which used
        // to repeat the same number beside every name regardless of which row was open.
        <span className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
          {cost ? formatDuration(cost.duration_ms - cost.blocked_ms) : "Done"}
          <Check className="h-3.5 w-3.5 text-success" aria-hidden="true" />
        </span>
      )}
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

  return (
    <div className="space-y-3" role="tablist" aria-label="Users in this run">
      <div className="space-y-2">
        {mixed ? (
          <Segmented<"all" | "failed" | "ok">
            value={filter}
            onChange={setFilter}
            ariaLabel="Filter people by status"
            options={[
              { value: "all", label: `All ${results.length}` },
              { value: "failed", label: `Failed ${failedTotal}` },
              { value: "ok", label: `OK ${okTotal + skippedTotal}` },
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
      <div className="overflow-hidden rounded-lg border">
        <div className="max-h-96 divide-y divide-border/50 overflow-y-auto">
          {bothGroups && failed.length > 0 && (
            <GroupLabel>Failed · {failed.length}</GroupLabel>
          )}
          {failed.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
              cost={costBySlug?.get(result.slug)}
              built={builtBySlug?.get(result.slug)}
            />
          ))}
          {bothGroups && ok.length > 0 && (
            <GroupLabel>Succeeded · {ok.length}</GroupLabel>
          )}
          {ok.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
              cost={costBySlug?.get(result.slug)}
              built={builtBySlug?.get(result.slug)}
            />
          ))}
          {bothGroups && skipped.length > 0 && (
            <GroupLabel>Skipped · {skipped.length}</GroupLabel>
          )}
          {skipped.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
              cost={costBySlug?.get(result.slug)}
              built={builtBySlug?.get(result.slug)}
            />
          ))}
          {pending.length > 0 && (
            <GroupLabel>Pending · {pending.length}</GroupLabel>
          )}
          {pending.map((result) => (
            <UserRow
              key={result.slug}
              result={result}
              selected={selected}
              onSelect={onSelect}
              cost={costBySlug?.get(result.slug)}
              built={builtBySlug?.get(result.slug)}
            />
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
