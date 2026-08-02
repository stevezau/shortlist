import { Clock, Eye, Send, TrendingUp } from "lucide-react";
import { Link } from "react-router";

import { StatTile } from "@/components/stat-tile";
import { Skeleton } from "@/components/ui/skeleton";
import type { RowEffectiveness } from "@/lib/types";

/** A date someone can read, from an ISO string. */
function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

/** One library's landing rate, as a number and a bar.
 *
 * A row across two libraries is two Plex collections and they routinely perform differently — the
 * Movies half landing while the TV half does not is the single most actionable thing this panel can
 * say, and it is invisible in a combined figure.
 */
function LibraryBar({
  library,
  delivered,
  watched,
  rate,
}: RowEffectiveness["per_library"][number]) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="min-w-0 truncate">{library || "Library"}</span>
        <span className="shrink-0 font-medium tabular-nums">{pct(rate)}</span>
      </div>
      <div
        className="h-1.5 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`${library}: ${watched} of ${delivered} watched`}
      >
        <div
          className="h-full rounded-full bg-primary"
          style={{ width: `${Math.round((rate ?? 0) * 100)}%` }}
        />
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">
        {watched} of {delivered} watched
      </p>
    </div>
  );
}

/**
 * Whether this row is actually working, beside the settings that decide it.
 *
 * The rate is computed over a MATURED cohort — picks that have had their full 30 days to be watched
 * — and the panel refuses to show a rate before such a cohort exists. That refusal is the whole
 * point: a row delivered last night lands 0% for no reason but time, and a settings page reporting
 * that as failure would send someone to change settings that were never the problem. Three states,
 * and the difference between them is what makes the number trustworthy:
 *
 * 1. nothing delivered yet — say so, offer nothing else;
 * 2. delivered but nothing matured — show the size, say plainly it is too early to judge;
 * 3. a matured cohort — the rate, with the date it is measured to.
 */
export function RowEffectivenessPanel({
  data,
  isLoading,
  rowId,
}: {
  data: RowEffectiveness | undefined;
  isLoading: boolean;
  rowId: number;
}) {
  return (
    <div className="space-y-4 rounded-lg border bg-card p-5">
      <h2 className="text-sm font-medium">How this row is doing</h2>

      {isLoading || !data ? (
        <Skeleton className="h-24 w-full" />
      ) : data.first_delivered_at === null ? (
        <p className="text-sm text-muted-foreground">
          This row hasn’t delivered anything yet. Once it runs, this is where
          you’ll see whether people are watching what it picks.
        </p>
      ) : data.matured === null ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <StatTile
              icon={Send}
              label="Delivered"
              value={data.delivered}
              hint="titles put in a row"
            />
            <StatTile
              icon={Eye}
              label="Watched"
              value={data.watched}
              hint="so far"
            />
          </div>
          <p className="text-sm text-muted-foreground">
            Too early for a score. A pick counts as a hit if it’s watched within{" "}
            {data.matured_days} days, and none of these have had that long yet.
          </p>
        </>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
            <StatTile
              icon={TrendingUp}
              label="Hit rate"
              value={pct(data.matured.rate)}
              hint="of judged picks watched"
              title={`Picks watched within ${data.matured_days} days, as a share of the picks old enough to judge.`}
            />
            <StatTile
              icon={Eye}
              label="Watched"
              value={data.matured.watched}
              hint={`within ${data.matured_days} days`}
            />
            <StatTile
              icon={Clock}
              label="Judged on"
              value={data.matured.delivered}
              hint="picks old enough"
              title="Only picks that have had their full window count towards the rate. Newer ones are excluded so recency can't look like failure."
            />
            <StatTile
              icon={Send}
              label="Delivered"
              value={data.delivered}
              hint="all time"
            />
          </div>

          {/* Only when there is more than one — a single bar restates the headline. */}
          {data.per_library.length > 1 && (
            <div className="space-y-3 border-t pt-3">
              {data.per_library.map((lib) => (
                <LibraryBar key={lib.library} {...lib} />
              ))}
            </div>
          )}

          <p className="text-xs text-muted-foreground">
            Counted over picks delivered before{" "}
            {shortDate(data.matured.cohort_to)}, so every one has had its full{" "}
            {data.matured_days} days. Newer picks are in the all-time total but
            not the score.
          </p>
        </>
      )}

      <Link
        to={`/runs?row=${rowId}`}
        className="inline-block text-sm text-primary underline-offset-4 hover:underline"
      >
        See this row’s runs
      </Link>
    </div>
  );
}
