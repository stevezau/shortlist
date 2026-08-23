import {
  CalendarClock,
  CheckCircle2,
  CircleSlash,
  Clock,
  RefreshCw,
  Send,
  Trash2,
  TrendingUp,
  Users as UsersIcon,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Engagement } from "@/components/dashboard/engagement";
import { QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { StatTile } from "@/components/stat-tile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatHitRate, timeAgo, weekStarting } from "@/lib/format";
import {
  useClearDeletedRows,
  useDeletedRows,
  useReport,
  useSyncWatched,
} from "@/lib/queries";
import type { EffectivenessReport, ReportWindow } from "@/lib/types";
import { cn } from "@/lib/utils";

const WINDOW_OPTIONS: { value: ReportWindow; label: string }[] = [
  { value: "7", label: "7 days" },
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "all", label: "All time" },
];

const WINDOW_PHRASE: Record<ReportWindow, string> = {
  "7": "the last 7 days",
  "30": "the last 30 days",
  "90": "the last 90 days",
  all: "all time",
};

/** Shows when the daily watch-status sync last ran and next fires, with a manual "Sync now". */
function WatchSyncLine({ sync }: { sync: EffectivenessReport["watch_sync"] }) {
  const syncNow = useSyncWatched();
  // Disabled only while the request is actually in flight — it used to also stay disabled (and
  // stuck reading "Syncing…") forever after a SUCCESSFUL sync, with no way to run it again short of
  // reloading the page, and no way to tell a failure from success at all.
  const label = syncNow.isPending
    ? "Syncing…"
    : syncNow.isError
      ? "Try again"
      : "Sync now";
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span>
        Watch status{" "}
        {sync.last ? `synced ${timeAgo(sync.last)}` : "not synced yet"}
        {/* "and on every run" is not filler: the sync job's interval is normally LONGER than the
            run schedule, so "next check <in 3 days>" on its own reads as "this data is three days
            out of date" when a run tonight will refresh it. */}
        {sync.next &&
          ` · next check ${formatDate(sync.next)}, and on every run`}
        .
      </span>
      <div className="flex items-center gap-2">
        {syncNow.isError && (
          <span role="alert" className="text-destructive-text">
            Couldn’t start the sync.
          </span>
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => syncNow.mutate()}
          disabled={syncNow.isPending}
        >
          <RefreshCw aria-hidden="true" />
          {label}
        </Button>
      </div>
    </div>
  );
}

/**
 * Change vs the previous equal period, as a hint line.
 *
 * `lowerIsBetter` for "days to watch": a drop there is an improvement, and colouring it red because
 * the number went down would read exactly backwards.
 */
function Delta({
  value,
  reportWindow,
  suffix = "",
  lowerIsBetter = false,
}: {
  value: number | null;
  reportWindow: ReportWindow;
  suffix?: string;
  lowerIsBetter?: boolean;
}) {
  if (reportWindow === "all") return <>all time</>;
  if (value === null || value === 0) {
    return (
      <>vs previous {WINDOW_PHRASE[reportWindow].replace("the last ", "")}</>
    );
  }
  const up = value > 0;
  const good = lowerIsBetter ? !up : up;
  return (
    <span className={good ? "text-success" : "text-muted-foreground"}>
      {up ? "▲" : "▼"} {up ? "+" : "−"}
      {Math.abs(value)}
      {suffix} vs previous
    </span>
  );
}

/**
 * A tiny watches-per-week bar chart — no library, just normalized divs.
 *
 * The count is readable. It used to hang off a native `title` on the BAR, which made the hover
 * target the drawn rectangle: a quiet week is a ~3px sliver glued to the bottom of an 80px box, so
 * most of each column hit nothing, and even a direct hit needed a second of stillness to pay out.
 * Each week is now a full-height column that reports into a readout line under the chart, and the
 * two ends of the axis are labelled — 16 unnamed bars said nothing about *when*.
 */
function Trend({ trend }: { trend: EffectivenessReport["trend"] }) {
  // Which week the pointer is over; null falls back to the latest week, so the readout says
  // something useful on a touch screen, where there is no hover at all.
  const [hovered, setHovered] = useState<string | null>(null);
  const max = Math.max(1, ...trend.map((t) => t.watched));
  if (trend.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        No watches recorded yet — this fills in as people watch their picks.
      </p>
    );
  if (trend.length < 3)
    return (
      <div className="flex h-20 flex-col items-center justify-center gap-1">
        <p className="text-2xl font-semibold tabular-nums">
          {trend.reduce((s, t) => s + t.watched, 0)}
        </p>
        <p className="text-xs text-muted-foreground">
          watched this week — chart fills in after a few weeks
        </p>
      </div>
    );
  const total = trend.reduce((s, t) => s + t.watched, 0);
  const first = trend[0];
  const last = trend[trend.length - 1];
  // Unreachable — the `< 3` return above guarantees three entries. It is here because
  // `noUncheckedIndexedAccess` types an index read as possibly-undefined, and narrowing once here
  // beats threading `first &&` / `last &&` through every line of the markup below.
  if (!first || !last) return null;
  const shown = trend.find((t) => t.week === hovered) ?? last;

  return (
    <div className="space-y-1.5">
      {/* The chart is aria-hidden (hover reaches a mouse and nothing else), so a screen reader gets
          NOTHING from it without a text alternative — this is that alternative. */}
      <p className="sr-only">
        {total} watched across the last {trend.length} weeks, of which{" "}
        {trend.reduce((s, t) => s + t.finished, 0)} were finished. From{" "}
        {first.watched} in the week of {weekStarting(first.week)} to{" "}
        {last.watched} in the week of {weekStarting(last.week)}.
      </p>

      {/* The readout the hover feeds. Not a live region: the sr-only line above already carries the
          whole series, and announcing a new week on every pixel of mouse travel is noise. With
          nothing hovered it names the latest week, so it still says something on a touch screen —
          where there is no hover to give at all. */}
      <p
        aria-hidden="true"
        className="flex flex-wrap items-baseline gap-x-1.5 text-xs text-muted-foreground"
      >
        <span className="font-medium tabular-nums text-foreground">
          {shown.watched}
        </span>
        watched in the week of {weekStarting(shown.week)}
        <span className="opacity-70">
          · {shown.finished} finished
          {shown.watched > shown.finished &&
            `, ${shown.watched - shown.finished} still going`}
        </span>
        {hovered === null && <span className="opacity-70">· latest</span>}
      </p>

      <div
        className="flex h-20 items-stretch gap-1"
        aria-hidden="true"
        onMouseLeave={() => setHovered(null)}
      >
        {trend.map((t) => {
          // The floor applies to the COLUMN, then the two segments split it proportionally —
          // flooring each segment instead would draw a 4% "finished" block for a week that
          // finished nothing, which is a lie about the data at the exact size hardest to notice.
          const columnPct = Math.max(4, (t.watched / max) * 100);
          const finishedPct =
            t.watched > 0 ? columnPct * (t.finished / t.watched) : 0;
          return (
            // The COLUMN is the hover target, not the bar it contains: `justify-end` drops the bar to
            // the bottom of a full-height box, so a week with two watches is still readable from the
            // 77px of empty space above its 3px bar.
            <div
              key={t.week}
              data-testid="trend-week"
              onMouseEnter={() => setHovered(t.week)}
              className={cn(
                "flex flex-1 cursor-default flex-col justify-end rounded-t transition-colors",
                hovered === t.week ? "bg-muted" : "hover:bg-muted/60",
              )}
            >
              {/* Two segments of ONE hue rather than two colours: finished and still-going are an
                ordered pair, not two categories, so intensity carries the order. The finished part
                sits on the baseline where it can be compared across weeks by eye. `finished` is
                bucketed by the same week key as `watched` (see report_service), so it can never
                exceed the bar it is drawn inside. */}
              <div
                className={cn(
                  "rounded-t transition-colors",
                  hovered === t.week ? "bg-primary/50" : "bg-primary/30",
                )}
                style={{ height: `${columnPct - finishedPct}%` }}
              />
              <div
                className={cn(
                  "transition-colors",
                  finishedPct >= columnPct && "rounded-t",
                  hovered === t.week ? "bg-primary" : "bg-primary/70",
                )}
                style={{ height: `${finishedPct}%` }}
              />
            </div>
          );
        })}
      </div>

      {/* The axis. Only weeks with a watch get a bucket (`report_service` groups over rows that
          exist), so the bars are not evenly spaced in time — naming both ends is what stops the
          chart being read as sixteen consecutive weeks when it might span twenty. */}
      <div
        aria-hidden="true"
        className="flex justify-between text-xs text-muted-foreground/80"
      >
        <span>{weekStarting(first.week)}</span>
        <span>{weekStarting(last.week)}</span>
      </div>
    </div>
  );
}

/**
 * One line in a breakdown: a bar scaled to the BIGGEST value in its own list, and the count.
 *
 * Not a percentage of anything. The bar used to be a share of a 0–100% hit rate, so real values
 * (0–3%) were a one-pixel sliver on every row and the chart said nothing. Scaling to the list's own
 * maximum is what makes "Luke watched four times what Cassie did" visible at a glance.
 */
function CountBar({
  watched,
  finished,
  delivered,
  max,
}: {
  watched: number;
  finished: number;
  delivered: number;
  max: number;
}) {
  return (
    // Label FIRST, bar last. The label sizes itself and never wraps; the bar is the fixed-width
    // element, so it is the bars' right edges that line up down the list. Sizing the label instead
    // meant picking a width — and any width is wrong for some count: w-32 wrapped "3 watched · 103
    // delivered" onto two lines, and even w-44 overflows once a row passes 999 watched or 9999
    // delivered. This way no count can break the layout.
    <div className="flex min-w-0 items-center gap-2 xl:shrink-0">
      {/* Two labelled numbers, NOT "{watched} of {delivered}". They are counts over two different
          sets — watched-in-window and delivered-in-window — so presenting them as a fraction makes
          "4 of 0" reachable whenever delivery paused (a weekly row cron on a 7-day window). That is
          the same misleading fraction this rewrite exists to remove. */}
      {/* `nowrap` from `xl`, and never the element that shrinks. Below `xl` the caller stacks this
          under the row name, so it has the whole card width to itself; from `xl` the name is the
          flexible half and this is the fixed one. Letting BOTH be flexible was the old bug: flex
          shares a deficit in proportion to content width, so the long counts label kept ~180px and
          the row name was squeezed to 32px — "Late Night" rendered as "Lat…" at every width from
          390 to 1280. Measured, not theorised.

          The nowrap stays breakpoint-gated even though the label now has a line to itself, because
          a line to itself is not the same as room: at 320px a card's content box is 238px, and
          "1203 watched · 318 finished · 41600 delivered" is 305px. Unconditional `nowrap` removes
          the only break opportunity in the label — the separators are real whitespace text nodes
          in THIS span — and the page scrolls sideways again, 355 against a 320 client. The shrink
          fix lives in `xl:flex-1` / `xl:shrink-0`, not here, so gating this costs nothing. */}
      <span className="text-right tabular-nums text-muted-foreground xl:whitespace-nowrap">
        {/* "watched" stays the leading number so the list keeps its old meaning and its old sort.
            "finished" is the qualifier beside it: a series counts as watched on its first episode,
            so a big watched number with a small finished one means sampled, not enjoyed — which is
            exactly what a single count could never say. */}
        {/* Each clause is its own nowrap span so a narrow screen breaks BETWEEN clauses rather than
            between a number and its noun — "0" on one line and "finished" on the next reads as a
            different, missing figure.

            The separators sit OUTSIDE the spans, as real whitespace text nodes. Putting " · " inside
            a nowrap span (the obvious way to write this) leaves no whitespace between the spans at
            all, so the browser has no break opportunity anywhere in the label and the whole thing
            behaves as one unbreakable string — measured: 404px of it inside a 390px phone. */}
        <span className="whitespace-nowrap">
          <span className="font-medium text-foreground">{watched}</span> watched
        </span>
        {watched > 0 && (
          <>
            {" "}
            <span className="whitespace-nowrap">
              · <span className="font-medium text-foreground">{finished}</span>{" "}
              finished
            </span>
          </>
        )}
        {/* "delivered", not "sent" — the Requests card on this same page uses "sent" to mean asked of
            Sonarr/Radarr, and two meanings of the word side by side is exactly the kind of quiet
            ambiguity this rewrite is meant to remove. */}
        {delivered > 0 && (
          <>
            {" "}
            <span className="whitespace-nowrap">{`· ${delivered} delivered`}</span>
          </>
        )}
      </span>
      {/* `2xl`, not `xl`. These cards sit in a 2-column grid from `lg`, so a card is ~360px at
          1024 and ~500px at 1280 — and `xl` IS 1280, which is to say the bar switched itself on at
          the exact width where the last 96px did not exist. Measured: it landed at right:1290 in a
          1280px viewport and scrolled the whole document sideways. The numbers carry the
          information on their own; the bar only earns its width once a card is wide enough that
          nothing has to be given up for it. */}
      {/* One bar, split: the solid part is what got finished, the faded part what is still going.
          Same hue at two intensities because the two are ordered, not two categories — and the
          finished part is anchored at the left so it can be compared down the list by eye. */}
      <div className="hidden h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-muted 2xl:flex">
        <div
          className="h-full bg-primary"
          style={{ width: `${max > 0 ? (finished / max) * 100 : 0}%` }}
        />
        {/* /50, not /35: at /35 this segment sat almost on top of the `bg-muted` track behind it,
            so "watched but not finished" and "never watched" looked the same at a glance — which is
            the one distinction this bar exists to draw. */}
        <div
          className="h-full bg-primary/50"
          style={{
            width: `${max > 0 ? (Math.max(0, watched - finished) / max) * 100 : 0}%`,
          }}
        />
      </div>
    </div>
  );
}

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    // `min-w-0` because this Card is a GRID ITEM, and a grid item's default `min-width: auto`
    // resolves to its min-content width. Without it the card sized itself to its widest line (508px
    // on a 358px column), overflowed the page, and — because it then had room to spare — nothing
    // inside ever truncated. The dashboard scrolled 134px sideways on a phone.
    <Card className="min-w-0">
      <CardContent className="space-y-3 pt-6">
        <div>
          <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
          {hint && (
            <p className="mt-0.5 text-xs text-muted-foreground/80">{hint}</p>
          )}
        </div>
        {children}
      </CardContent>
    </Card>
  );
}

/**
 * A collapsed-by-default section: a one-line toggle, expanding to `children`.
 *
 * `ZeroDisclosure` and `DeletedRows` were the same widget wearing different copy — a button that
 * flips "›"/"▾" and reveals a list underneath. This is that widget; each caller supplies only what
 * makes it theirs (the label, and — for `DeletedRows` — the delete-history UI alongside its list).
 */
function Disclosure({
  label,
  openLabel,
  children,
}: {
  /** Button text while collapsed. */
  label: string;
  /** Button text while open, if different (defaults to `label`). */
  openLabel?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-1.5 border-t pt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {open ? "▾" : "›"} {open ? (openLabel ?? label) : label}
      </button>
      {open && <div className="space-y-1.5">{children}</div>}
    </div>
  );
}

/** Rows with nothing in the window, folded away behind a count.
 *
 *  Seven of ten people reading "0" is a wall of empty bars that says nothing. It is still true, and
 *  still one click away — it just isn't the first thing the page shows you. */
function ZeroDisclosure({
  count,
  noun,
  plural,
  children,
}: {
  count: number;
  noun: string;
  plural: string;
  children: React.ReactNode;
}) {
  if (count === 0) return null;
  return (
    <Disclosure
      label={`${count} ${count === 1 ? noun : plural} with none in this window`}
    >
      {children}
    </Disclosure>
  );
}

function ByPerson({
  people,
  reportWindow,
}: {
  people: EffectivenessReport["per_user"];
  reportWindow: ReportWindow;
}) {
  const active = people.filter((p) => p.watched > 0);
  const idle = people.filter((p) => p.watched === 0);
  const max = Math.max(1, ...active.map((p) => p.watched));
  // First 10 are shown outright; anyone past that used to just vanish with no count and no way to
  // see them — the exact asymmetry ZeroDisclosure already fixed for the IDLE half of this list.
  const shown = active.slice(0, 10);
  const overflow = active.slice(10);

  const line = (p: EffectivenessReport["per_user"][number]) => (
    <div
      key={p.slug}
      className="flex flex-col gap-0.5 text-sm xl:flex-row xl:items-center xl:justify-between xl:gap-3"
    >
      {/* `min-w-0` is what makes `truncate` actually truncate here. `truncate` sets
          `white-space: nowrap`, so this flex child's min-content width is the WHOLE name — without
          `min-w-0` it refuses to shrink, and a long name pushes the line past a phone's screen
          instead of ellipsing (the dashboard scrolled 134px sideways at 390px). */}
      {/* `flex-1`: the name is the half that gets whatever room is left, so it only ellipses when
          the card genuinely cannot hold it. Below `xl` the line stacks instead. `lg` was measured
          and rejected: that is exactly where these cards go two-across, so a card is ~360px and the
          counts alone want ~290 of it — the name came out at 55px, worse than before the fix. The
          two only fit side by side once a card is ~500px, which is `xl`. */}
      <span className="min-w-0 truncate xl:flex-1">
        {p.display_name || p.username}
      </span>
      <CountBar
        watched={p.watched}
        finished={p.finished}
        delivered={p.delivered}
        max={max}
      />
    </div>
  );

  return (
    <Section
      title="By person"
      // "Most watched first" is load-bearing here, not decoration: only the top ten are shown
      // outright, so without it the fold looks arbitrary rather than like the bottom of a ranking.
      hint={`Most watched first · ${WINDOW_PHRASE[reportWindow]}`}
    >
      {active.length === 0 && idle.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nobody was delivered a pick in this window.
        </p>
      ) : (
        <>
          <div className="space-y-1.5">{shown.map(line)}</div>
          {active.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nobody watched a pick in this window.
            </p>
          )}
          {/* Positional, not a fresh claim. This is the TAIL of the list above — people 11 and
              beyond in the same watched-count ranking — and labelling it "N more people watched
              something" reused the section's own verb, so it read as a second, different finding
              sitting under the first. It is one list, shown ten at a time. */}
          {overflow.length > 0 && (
            <Disclosure
              label={`Show ${overflow.length} more ${overflow.length === 1 ? "person" : "people"}`}
              openLabel={`Hide ${overflow.length} more ${overflow.length === 1 ? "person" : "people"}`}
            >
              {overflow.map(line)}
            </Disclosure>
          )}
          <ZeroDisclosure count={idle.length} noun="person" plural="people">
            {idle.map(line)}
          </ZeroDisclosure>
        </>
      )}
    </Section>
  );
}

function ByRow({
  rows,
  reportWindow,
}: {
  rows: EffectivenessReport["per_row"];
  reportWindow: ReportWindow;
}) {
  // Deleted rows are kept — those watches really happened and still count in the totals — but they
  // are history, not something you can act on, so they don't get to crowd out the live rows.
  const live = rows.filter((r) => !r.deleted);
  const gone = rows.filter((r) => r.deleted);
  const max = Math.max(1, ...rows.map((r) => r.watched));

  const line = (r: EffectivenessReport["per_row"][number]) => (
    <div
      key={`${r.slug}-${r.section_key}-${r.library}`}
      className="flex flex-col gap-0.5 text-sm xl:flex-row xl:items-center xl:justify-between xl:gap-3"
    >
      <span className="flex min-w-0 items-center gap-1.5 xl:flex-1">
        {/* `min-w-0` for the same reason as ByPerson above: `truncate` alone cannot shrink a flex
            child, so the row name held the line open past the screen. */}
        <span
          className={`min-w-0 truncate ${r.deleted ? "text-muted-foreground" : ""}`}
        >
          {r.name}
        </span>
        {/* A row across >1 library is one collection per library. A {library_name} name
            already reads "✨ Movies …"; otherwise tag which library this line is. */}
        {r.library && !r.name.includes(r.library) && (
          <Badge variant="secondary" className="shrink-0 font-normal">
            {r.library}
          </Badge>
        )}
      </span>
      <CountBar
        watched={r.watched}
        finished={r.finished}
        delivered={r.delivered}
        max={max}
      />
    </div>
  );

  return (
    <Section
      title="By row"
      hint={`Most watched first · ${WINDOW_PHRASE[reportWindow]}`}
    >
      {live.length === 0 && gone.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No row delivered a pick in this window.
        </p>
      ) : (
        <>
          <div className="space-y-1.5">{live.map(line)}</div>
          {gone.length > 0 && (
            <DeletedRows
              count={gone.length}
              windowLabel={
                reportWindow === "all" ? "" : WINDOW_PHRASE[reportWindow]
              }
            >
              {gone.map(line)}
            </DeletedRows>
          )}
        </>
      )}
    </Section>
  );
}

/** Deleted rows, folded away — with a way to actually be rid of them.
 *
 *  Hiding is the default because their picks are real history that still counts in every total above.
 *  But "hidden for ever" is not the same as "gone", and a throwaway test row should not haunt the
 *  dashboard permanently, so clearing is offered too — explicitly, with what it costs stated. */
function DeletedRows({
  count,
  windowLabel,
  children,
}: {
  count: number;
  /** The window the lines above cover, or "" when they already cover all time. */
  windowLabel: string;
  children: React.ReactNode;
}) {
  const [confirming, setConfirming] = useState(false);
  const history = useDeletedRows();
  const clear = useClearDeletedRows();
  const totalPicks = (history.data ?? []).reduce((n, r) => n + r.picks, 0);
  const noun = count === 1 ? "row" : "rows";

  return (
    <Disclosure
      label={`Show ${count} deleted ${noun}`}
      openLabel={`Hide ${count} deleted ${noun}`}
    >
      <p className="text-xs text-muted-foreground/80">
        These rows were removed from Shortlist. Their picks still count in the
        totals above.
      </p>
      {children}
      {confirming ? (
        <div
          role="alert"
          className="mt-2 space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs"
        >
          <p className="text-foreground">
            Permanently delete the pick history of{" "}
            {count === 1 ? "this deleted row" : "these deleted rows"}?
          </p>
          {/* Name the all-time total, and why it exceeds the lines above. Clearing is never
              windowed, so on a 30-day view "20 picks" sits next to a visible 5 + 5 + 5 and reads
              as a bug unless the difference is said out loud. */}
          {totalPicks > 0 && (
            <p className="text-foreground">
              {totalPicks} picks in total
              {windowLabel && (
                <> &mdash; the lines above show only {windowLabel}</>
              )}
              .
            </p>
          )}
          {/* Say what it costs BEFORE asking. "The totals above" would under-warn: the same picks
              back each person's lifetime stats and their own pick history, so those drop too. */}
          <p className="text-muted-foreground">
            Their picks disappear from every total that counts them &mdash; here
            and on each person&rsquo;s page. This can&rsquo;t be undone. Rows
            that still exist are never touched.
          </p>
          <div className="flex gap-2">
            <Button
              variant="destructive"
              size="sm"
              loading={clear.isPending}
              onClick={() =>
                clear.mutate(undefined, {
                  onSuccess: () => setConfirming(false),
                })
              }
            >
              Delete the history
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirming(false)}
            >
              Keep it
            </Button>
          </div>
        </div>
      ) : (
        <Button
          variant="ghost"
          size="sm"
          className="mt-1 h-7 px-2 text-xs text-muted-foreground hover:text-destructive-text"
          onClick={() => setConfirming(true)}
        >
          <Trash2 className="h-3 w-3" aria-hidden />
          Delete their history
        </Button>
      )}
    </Disclosure>
  );
}

function ReportBody({
  report,
  reportWindow,
  onWindowChange,
}: {
  report: EffectivenessReport;
  reportWindow: ReportWindow;
  onWindowChange: (next: ReportWindow) => void;
}) {
  const { overall, coverage, runs, requests } = report;
  const { landing } = overall;

  // On a young install every window already covers all the data, so the numbers are identical
  // whichever button you press — a control that visibly does nothing reads as broken. Say why.
  // `since === null` is the "all time" window, which by definition can't be narrower than the data.
  const coversEverything =
    report.first_pick !== null &&
    report.since !== null &&
    new Date(report.first_pick) >= new Date(report.since);

  const selector = (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* h2, not h1 — PageHeader above already owns the page's h1 ("Dashboard"), and two of them
            leaves a screen reader with no page title at all. */}
        <h2 className="text-sm font-medium text-muted-foreground">Impact</h2>
        <Segmented
          value={reportWindow}
          onChange={onWindowChange}
          options={WINDOW_OPTIONS}
          ariaLabel="Report window"
        />
      </div>
      {coversEverything && (
        <p className="text-xs text-muted-foreground/80">
          Shortlist has only been recording since{" "}
          {formatDate(report.first_pick as string)}, so every window covers all
          of it — the numbers won&rsquo;t change until there&rsquo;s older
          history to leave out.
        </p>
      )}
    </div>
  );

  if (overall.delivered === 0 && overall.watched === 0) {
    return (
      <div className="space-y-4">
        {selector}
        <Card>
          <CardContent className="pt-6 text-sm text-muted-foreground">
            {runs.total === 0
              ? "Nothing has reached anyone's rows yet. Build them once from Runs — “Run all rows now” — and this page fills in as people start watching what Shortlist picked."
              : `Nothing reached a row, and nothing was watched, in ${WINDOW_PHRASE[reportWindow]}. Try a longer window.`}
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {selector}

      {/* Is it working? Counts for the window, each against the previous equal period. */}
      {/* SIX tiles now, which divides evenly at 2 and 3 — so the odd-one-out span the five-tile
          layout needed is gone. Six ACROSS still starts at `xl`, not `lg`: at 1024 the columns are
          narrow enough to wrap "PEOPLE WATCHING" and "TIME TO WATCH" onto two lines while their
          neighbours stay on one, leaving the row of tiles at different heights. */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <StatTile
          icon={TrendingUp}
          label="Watched"
          value={overall.watched}
          hint={
            <Delta value={overall.watched_delta} reportWindow={reportWindow} />
          }
          title="Picks people started watching in this window. A series counts here from its first episode, which is Plex's own definition — see Finished for the ones they saw out. A pick delivered earlier and watched now counts here: this is about watching, not delivery."
        />
        {/* Beside Watched, never instead of it. A series is credited as watched on one episode, so
            the two numbers together are what say whether a pick was enjoyed or merely sampled —
            measured on a real server, only 21 of 158 credited show picks had actually been
            finished. */}
        {/* No `Delta` here, unlike its neighbours, and that is deliberate: this window's finishes
            are counted as of now while the previous window's had a whole extra period to complete,
            so a steady server would render a permanent decline. The hint says what the number is a
            share OF instead, which is the comparison that actually reads. */}
        <StatTile
          icon={CheckCircle2}
          label="Finished"
          value={overall.finished}
          hint={
            overall.watched > 0
              ? `of ${overall.watched} watched`
              : "nothing watched yet"
          }
          title="Of the picks watched, the ones they saw out: a film played, or a series with every episode watched. Plex publishes no finished flag for a series, so this counts episodes."
        />
        {/* The one number here that Plex's watched flag cannot produce. A pick nobody opened and a
            pick someone played for three minutes are both "not watched" to Plex, and they say
            opposite things: one never got their attention, the other got it and lost it. Only live
            playback can tell them apart, so this tile is empty until the listener has seen some. */}
        <StatTile
          icon={CircleSlash}
          label="Dropped"
          value={overall.dropped + overall.bounced}
          tone={overall.dropped + overall.bounced > 0 ? "warning" : "default"}
          hint={
            overall.bounced > 0
              ? `${overall.bounced} barely started`
              : "started, never finished"
          }
          title="Films someone started and gave up on. FILMS ONLY: how far through an episode someone got does not tell us how far through a series they are, so shows are not counted here at all. Counted from live playback, so a title nobody has played since watch tracking started is not in here either. 'Barely started' is under 5% in: opened and closed, rather than a fair go."
        />
        <StatTile
          icon={UsersIcon}
          label="People watching"
          value={`${coverage.users_watched} of ${coverage.users_enabled}`}
          hint={
            <Delta
              value={coverage.users_watched_delta}
              reportWindow={reportWindow}
            />
          }
          title="People who watched at least one pick in this window, out of everyone currently enabled."
        />
        <StatTile
          icon={Clock}
          label="Time to watch"
          value={
            overall.avg_days_to_watch === null
              ? "—"
              : `${overall.avg_days_to_watch}d`
          }
          hint={
            <Delta
              value={overall.avg_days_to_watch_delta}
              reportWindow={reportWindow}
              suffix="d"
              lowerIsBetter
            />
          }
          title="Average days from a title first being recommended to it first being watched, over titles first watched in this window."
        />
        <StatTile
          icon={CalendarClock}
          label="Runs"
          value={runs.in_window}
          hint={runs.last_finished ? timeAgo(runs.last_finished) : "never"}
          tone={runs.errors_last ? "destructive" : "default"}
          title={`Runs started in this window. ${runs.total} in total since install.`}
        />
      </div>

      <WatchSyncLine sync={report.watch_sync} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Section
          title="Watches per week"
          hint="Always the last 16 weeks, whatever window is selected."
        >
          <Trend trend={report.trend} />
        </Section>

        <Section
          title="Picks that get watched"
          hint="The share of delivered picks watched while the row was still showing them."
        >
          {landing.rate === null ? (
            // Say what it is waiting FOR and when it arrives — not the rule and a cutoff date the
            // reader has to invert. "Needs picks delivered before <date>" reads as though it wants
            // OLD picks; what it actually needs is for the picks it has to get older.
            <p className="text-sm text-muted-foreground">
              Not enough time yet. Every pick gets {landing.matured_days} days
              to be watched before it counts, so a fair score needs picks that
              have had their full {landing.matured_days} days.{" "}
              {report.first_pick ? (
                <>
                  Your first picks landed{" "}
                  {formatDate(report.first_pick as string, { dateOnly: true })},
                  so this starts showing a score around{" "}
                  {formatDate(
                    new Date(
                      new Date(report.first_pick as string).getTime() +
                        landing.matured_days * 86400000,
                    ).toISOString(),
                    { dateOnly: true },
                  )}
                  .
                </>
              ) : (
                <>It appears once your earliest picks reach that age.</>
              )}
            </p>
          ) : (
            <div className="space-y-1.5">
              <p className="text-2xl font-semibold tabular-nums">
                {formatHitRate(landing.rate)}
              </p>
              <p className="text-sm text-muted-foreground">
                {landing.watched} of {landing.delivered} picks delivered
                {landing.cohort_from
                  ? ` between ${formatDate(landing.cohort_from)} and ${formatDate(landing.cohort_to)}`
                  : ` before ${formatDate(landing.cohort_to)}`}
                .
              </p>
              {/* The same cohort, the stricter question. Without it the headline percentage is the
                  one number on the page that still counts a single episode of a series as a win. */}
              <p className="text-sm text-muted-foreground">
                <span className="font-medium text-foreground tabular-nums">
                  {formatHitRate(landing.finished_rate)}
                </span>{" "}
                were finished ({landing.finished} of {landing.delivered}).
              </p>
              {/* One clause, not a paragraph. The reason a young pick is excluded (it has not had
                  its chance yet) follows from the rule itself; spelling the reasoning out was four
                  lines of prose under a single number. */}
              <p className="text-xs text-muted-foreground/80">
                Only picks that have had their full {landing.matured_days} days
                are counted.
              </p>
            </div>
          )}
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <ByPerson people={report.per_user} reportWindow={reportWindow} />
        <ByRow rows={report.per_row} reportWindow={reportWindow} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {report.top_titles.length > 0 && (
          <Section
            title="Most watched"
            hint={`Most watchers first · ${WINDOW_PHRASE[reportWindow]}`}
          >
            <ul className="space-y-1 text-sm">
              {report.top_titles.map((t) => (
                <li
                  key={`${t.tmdb_id}-${t.media_type}`}
                  className="flex items-center justify-between gap-3"
                >
                  <span className="truncate">{t.title}</span>
                  <span className="shrink-0 text-muted-foreground">
                    {t.watchers} {t.watchers === 1 ? "watcher" : "watchers"}
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        )}

        {requests.sent > 0 && (
          <Section
            title="Requests"
            hint={`Sent to Sonarr/Radarr in ${WINDOW_PHRASE[reportWindow]}.`}
          >
            <div className="flex items-center gap-2 text-sm">
              <Send className="h-4 w-4 text-muted-foreground" aria-hidden />
              <span>
                <span className="font-medium text-foreground">
                  {requests.sent}
                </span>{" "}
                sent ·{" "}
                <span className="font-medium text-foreground">
                  {requests.watched_after_sent}
                </span>{" "}
                watched since ·{" "}
                <span className="font-medium text-foreground">
                  {requests.pending}
                </span>{" "}
                awaiting approval
              </span>
            </div>
            <Link
              to="/requests?tab=sent"
              className="text-xs text-primary underline-offset-4 hover:underline"
            >
              View the full send log →
            </Link>
          </Section>
        )}
      </div>

      {report.recent.length > 0 && <RecentlyWatched recent={report.recent} />}

      {/* The detail behind the Dropped tile: who dropped what, and where people stop. Its own
          component because it is a separate request — the engagement scan is per-pick where the
          report above is aggregate, and making the dashboard wait on both would delay the numbers
          that are ready. */}
      <Engagement reportWindow={reportWindow} />
    </div>
  );
}

/** How many watches show before the rest are folded away. The server sends at most 20
 *  (`report_service._recent_watches`), so this list is bounded twice over and can never grow the
 *  page without limit — the fold is about what is worth reading at a glance, not about volume. */
const RECENT_SHOWN = 12;

/**
 * "watched", "finished" or "started" — the distinction the rest of this page already draws.
 *
 * A FILM keeps "watched": it has no middle state, so "finished" would add a word without adding a
 * fact, and "started" would be wrong for the overwhelmingly common case. A SERIES is credited by
 * Plex on its first finished episode, so "watched" there means only that they began it — measured
 * on a real server, 21 of 158 credited show picks had actually been seen out. Saying "watched" for
 * the other 137 overstates the result on the one page that exists to report it, and contradicts the
 * By-row card directly above, which has said "N watched · M finished" all along.
 */
function watchVerb(watch: EffectivenessReport["recent"][number]): string {
  if (watch.media_type !== "show") return "watched";
  return watch.finished_at ? "finished" : "started";
}

/**
 * The newest watches, newest first.
 *
 * The extras used to be `slice(0, 12)` and nothing else: the server sends up to 20, so eight of
 * them were dropped on the floor with no count, no disclosure and nothing on screen admitting the
 * list was capped at all — which reads as "this is everything that happened" when it is not.
 */
function RecentlyWatched({
  recent,
}: {
  recent: EffectivenessReport["recent"];
}) {
  const line = (
    w: EffectivenessReport["recent"][number],
    i: number,
  ): React.ReactNode => (
    <li
      // watched_at (when present) is a stable, unique-enough identity for this list;
      // falling back to the index only for the rare entry missing it.
      key={`${w.username}-${w.title}-${w.watched_at ?? i}`}
      className="flex flex-wrap items-baseline gap-x-2 text-muted-foreground"
    >
      <span className="font-medium text-foreground">
        {w.display_name || w.username}
      </span>
      {watchVerb(w)}
      <span className="text-foreground">{w.title}</span>
      <Badge variant="secondary" className="font-normal">
        {w.row}
      </Badge>
      {w.watched_at && <span>· {timeAgo(w.watched_at)}</span>}
    </li>
  );

  const shown = recent.slice(0, RECENT_SHOWN);
  const rest = recent.slice(RECENT_SHOWN);

  return (
    <Section
      title="Recently watched from Shortlist"
      // Says the list is bounded. Without it, a feed that stops at twenty reads as a complete
      // history of what people watched — and the dashboard has no other place that number appears.
      hint={`The ${recent.length === 1 ? "newest watch" : `newest ${recent.length} watches`}. Older ones are on each person's page.`}
    >
      <ul className="space-y-1 text-sm">{shown.map(line)}</ul>
      {rest.length > 0 && (
        <Disclosure
          label={`Show ${rest.length} more`}
          openLabel={`Hide ${rest.length} more`}
        >
          <ul className="space-y-1 text-sm">{rest.map(line)}</ul>
        </Disclosure>
      )}
    </Section>
  );
}

/**
 * The dashboard tracking report — what got watched, by whom, from which row, over a chosen window.
 *
 * Windowed on purpose. Every figure here used to be lifetime-cumulative, which made each ratio a
 * measure of how long Shortlist had been installed rather than of how good the picks were: a pick
 * stops being creditable once the row drops it, but the old denominator kept every pick ever
 * delivered, forever.
 */
export function ImpactReport() {
  // Named `reportWindow`, not `window` — the global `window` object shadowed here used to be one
  // character away from every reference inside this file and its children.
  const [reportWindow, setReportWindow] = useState<ReportWindow>("30");
  const report = useReport(reportWindow);
  return (
    <QueryBoundary
      query={report}
      skeleton={<Skeleton className="h-96 w-full" />}
    >
      {(data) => (
        <ReportBody
          report={data}
          reportWindow={reportWindow}
          onWindowChange={setReportWindow}
        />
      )}
    </QueryBoundary>
  );
}
