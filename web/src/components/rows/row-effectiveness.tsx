import { CalendarClock, Eye, History, Send, TrendingUp } from "lucide-react";

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
  finished,
  rate,
}: RowEffectiveness["per_library"][number]) {
  // Of what landed here, how much got seen out. A TV library finishes far less of what it lands
  // than a movie library does — a series is credited on its first episode — so this is the number
  // that stops "Movies beats TV" being read as a verdict on the row rather than on the medium.
  const finishedShare = watched > 0 ? Math.round((finished / watched) * 100) : 0;
  // Widths as whole percentages of the whole track, the second derived from the first so the pair
  // can never exceed `landedPct` (see the bar below).
  const landedPct = Math.round((rate ?? 0) * 100);
  const finishedPct =
    watched > 0 ? Math.round(landedPct * (finished / watched)) : 0;
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="min-w-0 truncate">{library || "Library"}</span>
        <span className="shrink-0 font-medium tabular-nums">{pct(rate)}</span>
      </div>
      <div
        className="flex h-1.5 overflow-hidden rounded-full bg-muted"
        role="img"
        aria-label={`${library}: ${watched} of ${delivered} watched, ${finished} finished`}
      >
        {/* One bar split by intensity, not two colours: finished and still-going are ordered.
            The second width is DERIVED from the first rather than rounded independently —
            `round(x) + round(100 - x)` reaches 101 whenever the split lands on .5, which overflows
            the track it is drawn inside. */}
        <div className="h-full bg-primary" style={{ width: `${finishedPct}%` }} />
        <div
          className="h-full bg-primary/50"
          // Clamped like `CountBar`'s equivalent. Unreachable while `finished <= watched` (the
          // server guards that), but the two bars should not disagree about whether it is possible.
          style={{ width: `${Math.max(0, landedPct - finishedPct)}%` }}
        />
      </div>
      <p className="text-xs text-muted-foreground tabular-nums">
        {watched} of {delivered} watched
        {watched > 0 && ` · ${finished} finished (${finishedShare}%)`}
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
  rowSlug,
}: {
  data: RowEffectiveness | undefined;
  isLoading: boolean;
  /** The row's SLUG, not its id: `/runs?row=` filters on the slug picks are stamped with. */
  rowSlug: string;
}) {
  const runsHref = `/runs?row=${encodeURIComponent(rowSlug)}`;
  return (
    <div className="space-y-4 rounded-lg border bg-card p-5">
      <h2 className="text-base font-semibold">How this row is doing</h2>

      {isLoading || !data ? (
        <Skeleton className="h-24 w-full" />
      ) : data.first_delivered_at === null ? (
        <p className="text-sm text-muted-foreground">
          This row hasn’t delivered anything yet. Once it runs, this is where
          you’ll see whether people are watching what it picks.
        </p>
      ) : data.matured === null ? (
        <>
          {/* Same track count as the matured case below, so the tiles are the same size whichever
              state the row is in — a strip that reflows when a cohort matures reads as a different
              panel rather than the same one with more to say. */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
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
              hint={`${data.finished} finished`}
              title="Watched counts a series from its first episode — Plex's own definition. Finished counts the ones seen out."
            />
            <StatTile
              icon={History}
              label="Runs"
              value={data.runs}
              hint="that built it"
              to={runsHref}
              title="Runs still on record that put something in this row. Older runs are cleared by the history retention setting."
            />
            <StatTile
              icon={CalendarClock}
              label="Last built"
              value={
                data.last_delivered_at ? shortDate(data.last_delivered_at) : "—"
              }
              hint="most recent delivery"
            />
          </div>
          <p className="text-sm text-muted-foreground">
            Too early for a score. A pick counts as a hit if it’s watched within{" "}
            {data.matured_days} days, and none of these have had that long yet.
          </p>
        </>
      ) : (
        <>
          {/* `sm`, not `xl`: this is a full-width strip at the top of the page now, so four tiles
              fit from tablet up. It was `xl` because it used to live in a 22rem sidebar. */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatTile
              icon={TrendingUp}
              label="Hit rate"
              value={pct(data.matured.rate)}
              hint="of judged picks watched"
              title={`Picks watched within ${data.matured_days} days, as a share of the picks old enough to judge.`}
            />
            {/* The finished count rides in the hint here too, not just in the pre-cohort branch —
                this is the state a real row spends its life in, and shipping the split to only the
                "too early to judge" panel would mean nobody ever saw it. */}
            <StatTile
              icon={Eye}
              label="Watched"
              value={data.matured.watched}
              hint={`${data.matured.finished} finished, within ${data.matured_days} days`}
              title="Watched counts a series from its first episode — Plex's own definition. Finished counts the ones seen out."
            />
            <StatTile
              icon={Send}
              label="Delivered"
              value={data.delivered}
              hint="all time"
            />
            <StatTile
              icon={History}
              label="Runs"
              value={data.runs}
              hint="that built it"
              to={runsHref}
              title="Runs still on record that put something in this row. Older runs are cleared by the history retention setting."
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
            Judged on {data.matured.delivered} picks — the ones delivered before{" "}
            {shortDate(data.matured.cohort_to)}, so every one has had its full{" "}
            {data.matured_days} days. Newer picks are in the all-time total but
            not the score.
          </p>
        </>
      )}
    </div>
  );
}
