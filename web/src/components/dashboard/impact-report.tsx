import { RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { NeedsALook, WHY_GAVE_UP } from "@/components/dashboard/engagement";
import { QueryBoundary } from "@/components/query-boundary";
import { TitleLinkIcons } from "@/components/title-link-icons";
import { TitlePoster } from "@/components/title-poster";
import { UserAvatar } from "@/components/user-avatar";
import { Why } from "@/components/why";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, timeAgo, weekStarting } from "@/lib/format";
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

/**
 * The manual "Sync now" control, on its own.
 *
 * Split out of a line that also printed when the sync last ran: that fact now lives in the verdict
 * card's status row beside the other health facts, and printing it twice on one screen was the kind
 * of duplication that later disagrees with itself.
 */
function WatchSyncButton() {
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
    <span className="flex items-center gap-2">
      {syncNow.isError && (
        <span role="alert" className="text-destructive-text">
          Couldn’t start the sync.
        </span>
      )}
      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending}
        className="flex items-center gap-1.5 rounded text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      >
        <RefreshCw className="h-3 w-3" aria-hidden="true" />
        {label}
      </button>
    </span>
  );
}

/**
 * The viewing share as text, computed from the counts rather than the pre-rounded ratio.
 *
 * Never "0.0%" while something came from a row: a share too small to show at one decimal is "<0.1%",
 * because a zero and a very small number say opposite things about whether the setup works.
 */
function sharePercent(
  share: EffectivenessReport["overall"]["viewing_share"],
): string {
  if (share.watched === 0) return "\u2014";
  const pct = (share.from_rows / share.watched) * 100;
  if (pct > 0 && pct < 0.05) return "<0.1%";
  return `${pct.toFixed(1)}%`;
}

/** A labelled rate with a bar under it. Two of these carry the whole "is it working" question. */
function Rate({
  label,
  value,
  detail,
  fill,
  tone = "primary",
  children,
}: {
  label: string;
  value: string;
  detail?: string;
  /** 0-100. Clamped to a visible sliver so a real-but-tiny rate is not indistinguishable from zero. */
  fill: number;
  tone?: "primary" | "success";
  children?: React.ReactNode;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs text-muted-foreground">{label}</span>
        <span className="text-sm font-medium tabular-nums">{value}</span>
      </div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full rounded-full",
            tone === "success" ? "bg-success" : "bg-primary",
          )}
          style={{
            width: `${Math.min(100, Math.max(fill > 0 ? 2 : 0, fill))}%`,
          }}
        />
      </div>
      {detail && (
        <p className="mt-1 text-xs tabular-nums text-muted-foreground">
          {detail}
        </p>
      )}
      {children}
    </div>
  );
}

type RunTone = "success" | "warning" | "destructive";

const RUN_DOT: Record<RunTone, string> = {
  success: "bg-success",
  warning: "bg-warning",
  destructive: "bg-destructive",
};

/**
 * Three tiers, not two — the same three `notifications.py` already draws for this exact fact
 * (`_last_run_problem`: whole-run `error`, per-user `warning`). `last_status` first, always: a run
 * that died usually errored some people on the way down, and reading the count first would repaint
 * a dead run amber.
 */
function runTone(runs: EffectivenessReport["runs"]): RunTone {
  if (runs.last_status === "error") return "destructive";
  return runs.errors_last > 0 ? "warning" : "success";
}

/** The words beside the dot, so the count is readable and not only colour-coded. */
function runOutcome(runs: EffectivenessReport["runs"]): string {
  if (runs.last_status === "error") return ", the run failed";
  if (runs.errors_last > 0) {
    return `, ${runs.errors_last} ${runs.errors_last === 1 ? "person" : "people"} failed`;
  }
  return ", no errors";
}

/**
 * Is it working? — the whole question, in one card.
 *
 * This replaced six stat tiles. Six equal boxes make six equal claims, and they were not equal: two
 * of them were health rather than impact (how long a watch takes, how many runs happened), and the
 * number that actually judges the setup — the share of delivered picks that got watched — was not a
 * tile at all. It sat mid-page under a chart, which is where the answer to "is this thing working"
 * had ended up.
 *
 * So the counts read as a sentence, the two RATES that judge the setup sit beside them, and the
 * health facts drop to a status line where they can be checked without competing.
 */
function Verdict({
  overall,
  coverage,
  runs,
  sync,
  reportWindow,
}: {
  overall: EffectivenessReport["overall"];
  coverage: EffectivenessReport["coverage"];
  runs: EffectivenessReport["runs"];
  sync: EffectivenessReport["watch_sync"];
  reportWindow: ReportWindow;
}) {
  const share = overall.viewing_share;
  const windowSuffix =
    reportWindow === "all" ? "since their rows started" : WINDOW_PHRASE[reportWindow];
  const gaveUp = overall.dropped + overall.bounced;
  const reach =
    coverage.users_enabled > 0
      ? (coverage.users_watched / coverage.users_enabled) * 100
      : 0;
  return (
    // Test ids, not class names. The e2e suite used to find these numbers by `div.rounded-lg.border`
    // — `StatTile`'s classes — so replacing the tiles with this card broke six assertions silently,
    // and they only surfaced once the SPA was rebuilt. A styling change must not be able to do that.
    <Card className="min-w-0" data-testid="verdict">
      <CardContent className="pt-6">
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,1fr)] lg:items-center">
          <div>
            <p className="flex items-baseline gap-2">
              <span
                className="text-4xl font-semibold leading-none tabular-nums"
                data-testid="verdict-watched"
              >
                {overall.watched}
              </span>
              <span className="text-sm text-muted-foreground">
                watched · {WINDOW_PHRASE[reportWindow]}
              </span>
              <Delta
                value={overall.watched_delta}
                reportWindow={reportWindow}
              />
            </p>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              <span
                className="font-medium text-primary tabular-nums"
                data-testid="verdict-finished"
              >
                {overall.finished}
              </span>{" "}
              finished them
              {/* Only when there is one. A dashboard that says "0 gave up" every day teaches you to
                  stop reading the line that matters on the day it is not zero. */}
              {gaveUp > 0 && (
                <>
                  {" · "}
                  <span className="font-medium text-destructive-text tabular-nums">
                    {gaveUp}
                  </span>{" "}
                  gave up part-way
                  {/* The SAME control the "Worth a look" card uses, not a `title` tooltip. Two
                      conventions for one fact shipped together, and the hover-only half does not
                      exist on the phone this app is read on. One sentence, one component. */}
                  <Why text={WHY_GAVE_UP} />
                </>
              )}
              {/* NO "N picks delivered" here.
                  It was a denominator nobody could divide by: `watched` is windowed on when the
                  watch happened, `delivered` on when the pick was CREATED, so the ratio the sentence
                  invited ("15,069 delivered, 38 finished") was never a rate of anything. The
                  rate that can be read sits immediately below as "Watched from Shortlist rows", and
                  reach is on the same card as "N of M people". A
                  five-figure count with no action attached to it only crowded both out. */}
            </p>
          </div>

          <div className="grid gap-4">
            <Rate
              label="Watched from Shortlist rows"
              // The dashboard's rate. It replaced "picks watched while their row still showed them",
              // which divided by every title ever SHOWN — mostly titles nobody will watch — and so sat
              // under 1% whether Shortlist worked or not (71 of 10,898 on a real server). What a row
              // competes for is what people actually watch. From the exact counts, not the rounded
              // `rate`, for the reason `sharePercent` gives.
              value={sharePercent(share)}
              fill={share.watched > 0 ? (share.from_rows / share.watched) * 100 : 0}
              detail={
                share.watched > 0
                  ? `${share.from_rows.toLocaleString()} of the ${share.watched.toLocaleString()} titles people watched were in their rows · ${windowSuffix}`
                  : undefined
              }
            >
              {share.watched === 0 && (
                // Says what the share counts, never that nobody watched. It reads the nightly watch sync while
                // the Watched figure above reads live credits, so a pick credited today can sit above an empty
                // share; on a new install everyone's history predates their rows; and on a server with only
                // shared rows it stays empty for good, so it must not promise a figure is on its way.
                <p className="mt-1 text-xs leading-snug text-muted-foreground">
                  Nothing to count yet. This counts people with a row of their
                  own, from their first pick, as the nightly watch sync records
                  what they watch.
                </p>
              )}
            </Rate>
            <Rate
              // "a pick", not "something": this counts people who watched a title FROM THEIR ROWS, and
              // beside a share of titles "watched something" read as the same measure twice. The detail
              // lines are what tell the pair apart — the first counts titles, this one counts people.
              label="People who watched a pick"
              value={`${coverage.users_watched} of ${coverage.users_enabled}`}
              fill={reach}
              tone="success"
              detail={`watched at least one title from their rows · ${windowSuffix}`}
            />
          </div>
        </div>

        {/* Health, not impact — and therefore a line rather than two tiles competing with the
            numbers above.

            It overlaps the health strip above the card on two facts (last run, live tracking) and
            is deliberately kept: these dots read raw report fields, where the strip reads the
            notification feed, which a dismissal can silence. When the two disagree, this line is
            right — so it stays, rather than being folded into the chip that can be switched off. */}
        <div className="mt-5 flex flex-wrap items-center gap-x-5 gap-y-1 border-t pt-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <span
              className={cn("h-1.5 w-1.5 rounded-full", RUN_DOT[runTone(runs)])}
              aria-hidden="true"
            />
            {runs.last_finished
              ? `Last run ${timeAgo(runs.last_finished)}${runOutcome(runs)}`
              : "No run yet"}
          </span>
          <span>
            Watch status{" "}
            {sync.last ? `synced ${timeAgo(sync.last)}` : "not synced yet"}
          </span>
          {/* The LIVE listener, beside the scheduled sync because they are two different mechanisms
              that fail independently. This one is the ONLY source of a partial watch — Plex's flag
              cannot see one — so while it is down the page quietly stops learning how far anyone
              gets, and every other number here carries on looking healthy. Nothing said whether it
              was up until an owner asked where that was shown. */}
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                "h-1.5 w-1.5 rounded-full",
                // Three states, not two. "Not started" was painted with the SAME green as "on",
                // so a listener that had never run once read as healthy — the one reading this
                // line exists to catch.
                sync.live_down_since
                  ? "bg-destructive"
                  : sync.live_since
                    ? "bg-success"
                    : "bg-muted-foreground/40",
              )}
              aria-hidden="true"
            />
            {sync.live_down_since
              ? `Live tracking down ${timeAgo(sync.live_down_since)}`
              : sync.live_since
                ? "Live tracking on"
                : "Live tracking not started"}
          </span>
          {overall.avg_days_to_watch !== null && (
            <span className="tabular-nums">
              Typically {overall.avg_days_to_watch} days from recommended to
              watched
            </span>
          )}
          <span className="ml-auto">
            <WatchSyncButton />
          </span>
        </div>
      </CardContent>
    </Card>
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
  // Null means the server found no previous period worth comparing against — it reached back past
  // the first pick Shortlist ever delivered, so the comparison would be against an uninstalled app.
  // Saying "vs previous 30 days" there is a dangling comparison; saying nothing hides why the arrow
  // is missing on a server that is simply too new.
  if (value === null) return <>no earlier period yet</>;
  if (value === 0) {
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
  if (trend.length === 0 || trend.every((week) => week.watched === 0))
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
      {/* A link, because "who is this person and what else did they get" is the next question this
          line provokes, and the answer is a page that already exists. `/users/:id` takes the id,
          which is why the report carries one — `slug` addresses nothing. `?tab=watched` lands on
          what they WATCHED: arriving from a watch figure onto their row list is a second click for
          something the click already asked for. */}
      <Link
        to={`/users/${p.id}?tab=watched`}
        className="min-w-0 truncate rounded-sm underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring xl:flex-1"
      >
        {p.display_name || p.username}
      </Link>
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
 *  Hiding is the default because their history is real and still counts in every total above.
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
        These rows were removed from Shortlist. Their history still counts in
        the totals above.
      </p>
      {children}
      {confirming ? (
        <div
          role="alert"
          className="mt-2 space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs"
        >
          <p className="text-foreground">
            Permanently delete the history of{" "}
            {count === 1 ? "this deleted row" : "these deleted rows"}?
          </p>
          {/* Name the all-time total, and why it exceeds the lines above. Clearing is never
              windowed, so on a 30-day view "20 records" sits next to a visible 5 + 5 + 5 and reads
              as a bug unless the difference is said out loud.

              "Records", not "picks": for a SHARED row the number counts watch credits, because a
              shared row writes no pick rows at all. Calling those picks is the noun drift the owner
              already rejected once elsewhere on this page. */}
          {totalPicks > 0 && (
            <p className="text-foreground">
              {totalPicks} history {totalPicks === 1 ? "record" : "records"} in
              total
              {windowLabel && (
                <> &mdash; the lines above show only {windowLabel}</>
              )}
              .
            </p>
          )}
          {/* Say what it costs BEFORE asking. "The totals above" would under-warn: the same picks
              back each person's lifetime stats and their own pick history, so those drop too. */}
          <p className="text-muted-foreground">
            Their history disappears from every total that counts it &mdash;
            here and on each person&rsquo;s page. This can&rsquo;t be undone.
            Rows that still exist are never touched.
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
            <div className="mt-3"><Button asChild variant="outline" size="sm"><Link to="/runs">Open Runs</Link></Button></div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {selector}

      {/* THE VERDICT, as one card that reads as a sentence.
          This was six stat tiles. Six equal boxes make six equal claims, and they are not equal: two
          of them (Time to watch, Runs) are health rather than impact, and the number that actually
          judges the setup — the share of delivered picks that got watched — was not among them at
          all. It sat mid-page under a chart. */}
      <Verdict
        overall={overall}
        coverage={coverage}
        runs={runs}
        sync={report.watch_sync}
        reportWindow={reportWindow}
      />

      {/* The verdict's rate is the only one on this page — two cards printing the same ratio at two
          different roundings (1% beside 0.5%) is how a dashboard comes to disagree with itself. */}
      <div className="grid gap-4 lg:grid-cols-2">
        <Section
          title="Watches per week · 16-week trend"
          hint="Fixed period: the last 16 weeks. The report window above applies to the other summaries."
        >
          <Trend trend={report.trend} />
        </Section>
        <ByRow rows={report.per_row} reportWindow={reportWindow} />
      </div>

      {/* Beside the people, because its first line is about them: how many were given picks and
          watched none. Every other line points at a row or a person elsewhere on this page.

          Requests stacks UNDER it, in the same column. By person is the tallest list on the page, so
          this column always had room to spare, and both cards are short summaries. Beside "Recently
          watched" (twenty lines) the Requests card used to float over a column of empty space. */}
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <ByPerson people={report.per_user} reportWindow={reportWindow} />
        <div className="grid min-w-0 content-start gap-4">
          <NeedsALook report={report} reportWindow={reportWindow} />
          {(requests.sent > 0 || requests.pending > 0) && (
            <RequestsSummary requests={requests} reportWindow={reportWindow} />
          )}
        </div>
      </div>

      {/* The two lists that grow get the full width, so their length never strands a card beside them. */}
      {report.top_titles.length > 0 && (
        <MostWatched titles={report.top_titles} reportWindow={reportWindow} />
      )}

      {report.recent.length > 0 && <RecentlyWatched recent={report.recent} />}

      {/* The detail behind the Dropped tile: who dropped what, and where people stop. Its own
          component because it is a separate request — the engagement scan is per-pick where the
          report above is aggregate, and making the dashboard wait on both would delay the numbers
          that are ready. */}
    </div>
  );
}

/** Sent, watched since, and waiting on you — each figure in its own tile, so none can sit in another's slot. */
function RequestsSummary({
  requests,
  reportWindow,
}: {
  requests: EffectivenessReport["requests"];
  reportWindow: ReportWindow;
}) {
  const tiles: { key: string; value: number; label: string; strong?: boolean }[] = [
    { key: "sent", value: requests.sent, label: "sent" },
    { key: "watched", value: requests.watched_after_sent, label: "watched since" },
    { key: "pending", value: requests.pending, label: "awaiting approval", strong: requests.pending > 0 },
  ];
  return (
    <Section
      title="Requests"
      // App-neutral on purpose — see run-stat-tiles: the route is a setting this card cannot see.
      hint={`Sent to be downloaded in ${WINDOW_PHRASE[reportWindow]}.`}
    >
      <dl className="grid grid-cols-3 gap-2">
        {tiles.map((tile) => (
          <div key={tile.key} className="rounded-md bg-elevated px-3 py-2" data-testid={`requests-${tile.key}`}>
            <dd
              className={cn(
                "text-xl font-semibold tabular-nums",
                tile.strong ? "text-primary" : "text-foreground",
              )}
            >
              {tile.value}
            </dd>
            <dt className="text-xs text-muted-foreground">{tile.label}</dt>
          </div>
        ))}
      </dl>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {requests.pending > 0 && (
          <Link to="/requests" className="text-primary underline-offset-4 hover:underline">
            Review {requests.pending} waiting →
          </Link>
        )}
        <Link to="/requests?tab=sent" className="text-primary underline-offset-4 hover:underline">
          View the full send log →
        </Link>
      </div>
    </Section>
  );
}

/**
 * The titles landing best, as a shelf of posters — the same thing Plex shows, so it reads at a glance.
 *
 * It used to be a bare "Ted Lasso · 10 watchers" list: nothing said what a title was, and nothing let
 * you look one up. Each title now carries its rank, poster, year, the newest few faces beside its
 * watcher count, and the TMDB/IMDb/Trakt links. Eight across on a wide screen; on a phone the shelf
 * scrolls sideways rather than stacking eight posters into one very tall column.
 */
function MostWatched({
  titles,
  reportWindow,
}: {
  titles: EffectivenessReport["top_titles"];
  reportWindow: ReportWindow;
}) {
  return (
    <Section title="Most watched" hint={`Most watchers first · ${WINDOW_PHRASE[reportWindow]}`}>
      <ul
        aria-label="Most watched"
        className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-2 sm:mx-0 sm:grid sm:grid-cols-4 sm:overflow-visible sm:px-0 sm:pb-0 lg:grid-cols-8"
      >
        {titles.map((t, i) => (
          <li key={`${t.tmdb_id}-${t.media_type}`} className="grid w-[104px] shrink-0 content-start gap-1.5 sm:w-auto">
            <div className="relative">
              <TitlePoster
                ratingKey={t.rating_key}
                className="aspect-[2/3] h-auto w-full rounded-md sm:h-auto sm:w-full"
              />
              <span
                className={cn(
                  "absolute left-1.5 top-1.5 rounded px-1.5 text-[11px] font-bold tabular-nums",
                  i === 0 ? "bg-primary text-primary-foreground" : "bg-black/65 text-primary",
                )}
              >
                {i + 1}
              </span>
            </div>
            <p className="truncate text-sm font-medium text-foreground" title={t.title}>
              {t.title}
            </p>
            <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
              {t.watcher_sample.length > 0 && (
                <span className="flex -space-x-1">
                  {t.watcher_sample.map((w) => (
                    <UserAvatar key={w.id} name={w.name} size="xs" labelled className="ring-2 ring-card" />
                  ))}
                </span>
              )}
              <span className="whitespace-nowrap tabular-nums">
                {t.watchers} {t.watchers === 1 ? "watcher" : "watchers"}
              </span>
            </div>
            <div className="flex items-center justify-between gap-2">
              {t.year != null ? (
                <span className="text-xs tabular-nums text-muted-foreground">{t.year}</span>
              ) : (
                <span />
              )}
              <TitleLinkIcons title={t} />
            </div>
          </li>
        ))}
      </ul>
    </Section>
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

/** "Today", "Yesterday", else "Fri 12 Sep" — the heading a run of watches is filed under. */
function dayLabel(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "Earlier";
  const day = new Date(iso);
  const midnight = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(now) - midnight(day)) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  return day.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

const VERB_BADGE: Record<string, string> = {
  finished: "bg-success/15 text-success",
  started: "bg-primary/15 text-primary",
  watched: "bg-secondary text-secondary-foreground",
};

/**
 * The newest watches, newest first, filed under their day.
 *
 * Each line used to be one run of text — person, verb, title, row, time — so the title, which is the
 * news, sat in the middle of a sentence. It now leads with the poster and title, says finished /
 * started / watched as a badge, puts who and which row on the line under it, and keeps the time and
 * the look-up links at the end. A day heading replaces "8h ago" as the thing that orders the list.
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
  ): React.ReactNode => {
    const verb = watchVerb(w);
    const name = w.display_name || w.username;
    return (
      <li
        // watched_at (when present) is a stable, unique-enough identity for this list;
        // falling back to the index only for the rare entry missing it.
        key={`${w.username}-${w.title}-${w.watched_at ?? i}`}
        className="grid grid-cols-[40px_minmax(0,1fr)] items-center gap-x-3 gap-y-1.5 py-2 sm:grid-cols-[40px_minmax(0,1fr)_auto]"
      >
        <TitlePoster ratingKey={w.rating_key} className="row-span-2 sm:row-span-1" />
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-sm">
            <span className="font-medium text-foreground">{w.title}</span>
            {w.year != null && (
              <span className="text-xs tabular-nums text-muted-foreground">{w.year}</span>
            )}
            <span className={cn("rounded-full px-2 text-[11px] font-semibold capitalize", VERB_BADGE[verb])}>
              {verb}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
            <UserAvatar name={name} size="xs" />
            {/* Linked when there is somebody to link to. `user_id` is null once they have left the
                server — the watch stays on record, so the line still renders, it just becomes plain
                text rather than a link to a page that would 404. */}
            {w.user_id !== null ? (
              <Link
                to={`/users/${w.user_id}?tab=watched`}
                className="rounded-sm font-medium text-foreground underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {name}
              </Link>
            ) : (
              <span className="font-medium text-foreground">{name}</span>
            )}
            <Badge variant="secondary" className="max-w-full truncate font-normal">
              {w.row}
            </Badge>
          </div>
        </div>
        <div className="col-start-2 flex items-center justify-between gap-3 sm:col-start-auto sm:flex-col sm:items-end sm:justify-center sm:gap-1.5">
          {/* How long ago — the owner prefers "1h ago" at a glance to a clock time. The exact time
              is one hover away, and the day heading above still files it under its day. */}
          {w.watched_at && (
            <time
              dateTime={w.watched_at}
              title={new Date(w.watched_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}
              className="whitespace-nowrap text-xs tabular-nums text-muted-foreground"
            >
              {timeAgo(w.watched_at)}
            </time>
          )}
          <TitleLinkIcons title={w} />
        </div>
      </li>
    );
  };

  const grouped = (watches: EffectivenessReport["recent"], label: string) => {
    const days: { day: string; watches: EffectivenessReport["recent"] }[] = [];
    for (const w of watches) {
      const day = dayLabel(w.watched_at);
      const last = days.at(-1);
      if (last && last.day === day) last.watches.push(w);
      else days.push({ day, watches: [w] });
    }
    return (
      <ul aria-label={label} className="space-y-1">
        {days.map(({ day, watches: dayWatches }) => (
          <li key={day}>
            <h3 className="pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground/80">
              {day}
            </h3>
            <ul className="divide-y divide-border/40">{dayWatches.map(line)}</ul>
          </li>
        ))}
      </ul>
    );
  };

  const shown = recent.slice(0, RECENT_SHOWN);
  const rest = recent.slice(RECENT_SHOWN);

  return (
    <Section
      title="Recently watched from Shortlist"
      // Says the list is bounded. Without it, a feed that stops at twenty reads as a complete
      // history of what people watched — and the dashboard has no other place that number appears.
      hint={`The ${recent.length === 1 ? "newest watch" : `newest ${recent.length} watches`}. Older ones are on each person's page.`}
    >
      {grouped(shown, "Recently watched from Shortlist")}
      {rest.length > 0 && (
        <Disclosure
          label={`Show ${rest.length} more`}
          openLabel={`Hide ${rest.length} more`}
        >
          {grouped(rest, "Older recent watches")}
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
