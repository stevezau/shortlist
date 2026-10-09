import { ChevronDown, ChevronRight, Trash2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link } from "react-router";

import { NeedsALook, WHY_GAVE_UP } from "@/components/dashboard/engagement";
import { panelRowClass, ReportPanel } from "@/components/dashboard/report-panel";
import { QueryBoundary } from "@/components/query-boundary";
import { TitlePoster } from "@/components/title-poster";
import { UserAvatar } from "@/components/user-avatar";
import { Why } from "@/components/why";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ReportSkeleton } from "@/components/dashboard/report-skeleton";
import { nextRowRun } from "@/lib/dashboard-status";
import { formatDate, timeAgo, timeUntil, weekStarting } from "@/lib/format";
import {
  useClearDeletedRows,
  useDeletedRows,
  useReport,
  useSchedule,
  useSyncWatched,
} from "@/lib/queries";
import type { EffectivenessReport, ReportWindow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { dayTime } from "@/lib/when";

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
    <>
      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending}
        className="rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      >
        {label}
      </button>
      {syncNow.isError && (
        <span role="alert" className="block text-[13px] font-normal text-destructive-text">
          Couldn’t start the sync.
        </span>
      )}
    </>
  );
}

/** One cell of the Impact strip: what it is, the figure, and one line under it. */
function Stat({ label, children, sub }: { label: string; children: ReactNode; sub?: ReactNode }) {
  return (
    <div className="min-w-0 bg-card px-4 py-3">
      <p className="text-xs font-semibold uppercase tracking-wider text-faint-foreground">{label}</p>
      <div className="mt-1 text-2xl font-semibold leading-tight tabular-nums">{children}</div>
      {sub && <div className="mt-0.5 space-y-0.5 text-[13px] leading-snug text-muted-foreground">{sub}</div>}
    </div>
  );
}

/** The quieter half of a figure: "· 100 delivered", "of 4". */
function Small({ children }: { children: ReactNode }) {
  return <span className="text-[13px] font-normal text-muted-foreground">{children}</span>;
}

/**
 * The watch sync in one line, for the Impact header: the live listener's state and a manual sync.
 *
 * The LIVE listener is what it reports, because it is the ONLY source of a partial watch — Plex's
 * flag cannot see one — so while it is down the page quietly stops learning how far anyone gets.
 * When the last scheduled sync ran is one hover away.
 */
function WatchSyncLine({ sync }: { sync: EffectivenessReport["watch_sync"] }) {
  return (
    <span
      className="inline-flex flex-wrap items-center gap-x-1.5 text-[13px] text-muted-foreground"
      title={sync.last ? `Last synced ${timeAgo(sync.last)}` : "Not synced yet"}
    >
      Watch sync:
      <span
        className={cn(
          "h-2 w-2 shrink-0 rounded-full",
          // Three states, not two: "not started" painted green read as healthy, the one reading
          // this line exists to catch.
          sync.live_down_since ? "bg-destructive" : sync.live_since ? "bg-success" : "bg-muted-foreground/40",
        )}
        aria-hidden="true"
      />
      <span>
        {sync.live_down_since
          ? `live tracking down ${timeAgo(sync.live_down_since)}`
          : sync.live_since
            ? "live"
            : "not started"}
      </span>
      <span aria-hidden="true">·</span>
      <WatchSyncButton />
    </span>
  );
}

/**
 * Is it working? — four facts in one strip under the Impact header.
 *
 * Each says in a line under it what it counts. The last run's health is the status strip's job
 * above, and the watch sync is in this panel's header, so neither repeats here.
 */
function Verdict({
  overall,
  coverage,
  reportWindow,
}: {
  overall: EffectivenessReport["overall"];
  coverage: EffectivenessReport["coverage"];
  reportWindow: ReportWindow;
}) {
  const gaveUp = overall.dropped + overall.bounced;
  return (
    // Test ids, not class names: the e2e suite reads these figures by id, so a styling change can
    // never silently break what it measures. `gap-px` on `bg-border` draws the hairlines.
    <div className="grid grid-cols-2 gap-px bg-border md:grid-cols-4">
      <Stat
        label="Watched from rows"
        sub={
          reportWindow !== "all" ? <Delta value={overall.watched_delta} reportWindow={reportWindow} /> : undefined
        }
      >
        <span data-testid="verdict-watched">{overall.watched}</span>{" "}
        {/* Two labelled counts, NOT "41 of 100". `watched` is windowed on when the watch happened and
            `delivered` on when the pick was created, so a fraction would claim a subset that isn't
            one — and reads "4 of 0" whenever delivery paused. The same rule as each row's line. */}
        <Small>· {overall.delivered.toLocaleString()} delivered</Small>
      </Stat>

      <Stat
        label="Finished"
        sub={
          // Only when there is one. A dashboard that says "0 gave up" every day teaches you to stop
          // reading the line that matters on the day it is not zero.
          gaveUp > 0 ? (
            <p>
              <span className="font-medium text-destructive-text tabular-nums">{gaveUp}</span> gave up part-way
              {/* The SAME control the "Worth a look" card uses: hover-only does not exist on a phone. */}
              <Why text={WHY_GAVE_UP} />
            </p>
          ) : (
            "watched to the end"
          )
        }
      >
        <span data-testid="verdict-finished">{overall.finished}</span>
      </Stat>

      <Stat
        // "a pick", not "something": this counts people who watched a title FROM THEIR ROWS.
        label="People watching"
        sub="watched a pick"
      >
        <span data-testid="verdict-reach">
          {coverage.users_watched} <Small>of {coverage.users_enabled}</Small>
        </span>
      </Stat>

      <Stat label="Time to watch" sub="typical, from pick to play">
        <span data-testid="verdict-time">
          {overall.avg_days_to_watch !== null ? (
            <>
              {overall.avg_days_to_watch} <Small>{overall.avg_days_to_watch === 1 ? "day" : "days"}</Small>
            </>
          ) : (
            "\u2014"
          )}
        </span>
      </Stat>
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
    <div className="flex flex-1 flex-col gap-1.5">
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
        className="flex min-h-20 flex-1 items-stretch gap-1"
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
              {/* Two segments of ONE neutral rather than two colours: finished and still-going are an
                ordered pair, not two categories, so intensity carries the order. The finished part
                sits on the baseline where it can be compared across weeks by eye. `finished` is
                bucketed by the same week key as `watched` (see report_service), so it can never
                exceed the bar it is drawn inside. */}
              <div
                className={cn(
                  "rounded-t transition-colors",
                  hovered === t.week ? "bg-muted-foreground/60" : "bg-muted-foreground/40",
                )}
                style={{ height: `${columnPct - finishedPct}%` }}
              />
              <div
                className={cn(
                  "transition-colors",
                  finishedPct >= columnPct && "rounded-t",
                  hovered === t.week ? "bg-foreground" : "bg-foreground/70",
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
 * One line in a breakdown: the name and its counts, and under them a bar scaled to the BIGGEST
 * value in its own list.
 *
 * Not a percentage of anything. The bar used to be a share of a 0–100% hit rate, so real values
 * (0–3%) were a one-pixel sliver on every row and the chart said nothing. Scaling to the list's own
 * maximum is what makes "Luke watched four times what Cassie did" visible at a glance. The bar is
 * neutral: amber marks the one action on a screen, not a data series.
 */
function CountLine({
  name,
  watched,
  finished,
  delivered,
  max,
}: {
  name: ReactNode;
  watched: number;
  finished: number;
  delivered: number;
  max: number;
}) {
  return (
    <div className={panelRowClass}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 text-sm">
        {/* `min-w-0` is what makes `truncate` actually truncate in a flex child. */}
        <span className="min-w-0 truncate font-medium">{name}</span>
        {/* Two labelled numbers, NOT "{watched} of {delivered}". They are counts over two different
            sets — watched-in-window and delivered-in-window — so a fraction makes "4 of 0" reachable
            whenever delivery paused. "finished" qualifies "watched": a series counts as watched on
            its first episode, so a big watched number with a small finished one means sampled, not
            enjoyed. Each clause is its own nowrap span so a narrow screen breaks BETWEEN clauses
            rather than between a number and its noun; the separators are real whitespace text nodes
            outside the spans, which is what gives the browser somewhere to break. */}
        <span className="text-[13px] tabular-nums text-muted-foreground">
          <span className="whitespace-nowrap">
            <span className="font-medium text-foreground">{watched}</span> watched
          </span>
          {watched > 0 && (
            <>
              {" "}
              <span className="whitespace-nowrap">
                · <span className="font-medium text-foreground">{finished}</span> finished
              </span>
            </>
          )}
          {/* "delivered", not "sent" — the Requests line uses "sent" for asks of Sonarr/Radarr. */}
          {delivered > 0 && (
            <>
              {" "}
              <span className="whitespace-nowrap">{`· ${delivered} delivered`}</span>
            </>
          )}
        </span>
      </div>
      {/* The solid part is what got finished, the faded part what is still going: one neutral at two
          intensities because the two are ordered, and the finished part is anchored left so it can
          be compared down the list by eye. */}
      <div
        data-testid="split-bar"
        className="mt-2 flex h-1 overflow-hidden rounded-full bg-secondary"
        aria-hidden="true"
      >
        <div className="h-full bg-muted-foreground" style={{ width: `${max > 0 ? (finished / max) * 100 : 0}%` }} />
        <div
          className="h-full bg-muted-foreground/40"
          style={{ width: `${max > 0 ? (Math.max(0, watched - finished) / max) * 100 : 0}%` }}
        />
      </div>
    </div>
  );
}

/** A summary panel on the dashboard — the shared {@link ReportPanel}, under its old name here. */
const Section = ReportPanel;

/**
 * A collapsed-by-default section: a one-line toggle, expanding to `children`.
 *
 * `ZeroDisclosure` and `DeletedRows` were the same widget wearing different copy — a button that
 * flips a chevron and reveals a list underneath. This is that widget; each caller supplies only what
 * makes it theirs (the label, and — for `DeletedRows` — the delete-history UI alongside its list).
 */
function Disclosure({
  label,
  openLabel,
  flush = false,
  children,
}: {
  /** Button text while collapsed. */
  label: string;
  /** Button text while open, if different (defaults to `label`). */
  openLabel?: string;
  /** Inside a flush panel's list: the toggle is a full-width line, and what it opens is more lines. */
  flush?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className={flush ? "border-t" : "space-y-1.5 border-t pt-2"}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "inline-flex items-center gap-1 rounded-sm text-[13px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          // Inset: the panel clips its corners, and a ring drawn outside a full-width line is cut off.
          flush && "w-full px-4 py-2.5 focus-visible:ring-inset sm:px-5",
        )}
      >
        <Chevron className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        {open ? (openLabel ?? label) : label}
      </button>
      {open && <div className={flush ? "divide-y border-t" : "space-y-1.5"}>{children}</div>}
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
      flush
      label={`${count} ${count === 1 ? noun : plural} with none in this window`}
    >
      {children}
    </Disclosure>
  );
}

function ByPerson({ people }: { people: EffectivenessReport["per_user"] }) {
  const active = people.filter((p) => p.watched > 0);
  const idle = people.filter((p) => p.watched === 0);
  const max = Math.max(1, ...active.map((p) => p.watched));
  // First 10 are shown outright; anyone past that used to just vanish with no count and no way to
  // see them — the exact asymmetry ZeroDisclosure already fixed for the IDLE half of this list.
  const shown = active.slice(0, 10);
  const overflow = active.slice(10);

  const line = (p: EffectivenessReport["per_user"][number]) => (
    <CountLine
      key={p.slug}
      name={
        // A link, because "who is this person and what else did they get" is the next question this
        // line provokes. `/users/:id` takes the id, which is why the report carries one — `slug`
        // addresses nothing. `?tab=watched` lands on what they WATCHED.
        <Link
          to={`/users/${p.id}?tab=watched`}
          className="rounded-sm underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {p.display_name || p.username}
        </Link>
      }
      watched={p.watched}
      finished={p.finished}
      delivered={p.delivered}
      max={max}
    />
  );

  return (
    <>
      {active.length === 0 && idle.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted-foreground sm:px-5">
          Nobody was delivered a pick in this window.
        </p>
      ) : (
        <>
          {shown.length > 0 && <div className="divide-y">{shown.map(line)}</div>}
          {active.length === 0 && (
            <p className="px-4 py-4 text-sm text-muted-foreground sm:px-5">
              Nobody watched a pick in this window.
            </p>
          )}
          {/* Positional, not a fresh claim. This is the TAIL of the list above — people 11 and
              beyond in the same watched-count ranking — and labelling it "N more people watched
              something" reused the section's own verb, so it read as a second, different finding
              sitting under the first. It is one list, shown ten at a time. */}
          {overflow.length > 0 && (
            <Disclosure
              flush
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
    </>
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
    <CountLine
      key={`${r.slug}-${r.section_key}-${r.library}`}
      name={
        <span className="flex min-w-0 items-center gap-1.5">
          <span className={cn("min-w-0 truncate", r.deleted && "text-muted-foreground")}>{r.name}</span>
          {/* A row across >1 library is one collection per library. A {library_name} name
              already reads "✨ Movies …"; otherwise tag which library this line is. */}
          {r.library && !r.name.includes(r.library) && (
            <Badge variant="secondary" className="shrink-0 font-normal">
              {r.library}
            </Badge>
          )}
        </span>
      }
      watched={r.watched}
      finished={r.finished}
      delivered={r.delivered}
      max={max}
    />
  );

  return (
    <>
      {live.length === 0 && gone.length === 0 ? (
        <p className="px-4 py-4 text-sm text-muted-foreground sm:px-5">
          No row delivered a pick in this window.
        </p>
      ) : (
        <>
          {live.length > 0 && <div className="divide-y">{live.map(line)}</div>}
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
    </>
  );
}

/**
 * Who is watching: the people, or the rows, in one panel with a switch between them.
 *
 * They were two panels printing the same counts over two groupings. The hint is load-bearing: only
 * the top ten people are shown outright, so without "most watched first" the fold looks arbitrary
 * rather than like the bottom of a ranking.
 */
function WhoIsWatching({ report, reportWindow }: { report: EffectivenessReport; reportWindow: ReportWindow }) {
  const [view, setView] = useState<"person" | "row">("person");
  return (
    <Section
      title="Who’s watching"
      hint={`Most watched first · ${WINDOW_PHRASE[reportWindow]}`}
      flush
      actions={
        <Segmented
          value={view}
          onChange={setView}
          ariaLabel="Group by"
          options={[
            { value: "person", label: "By person" },
            { value: "row", label: "By row" },
          ]}
        />
      }
    >
      {view === "person" ? (
        <ByPerson people={report.per_user} />
      ) : (
        <ByRow rows={report.per_row} reportWindow={reportWindow} />
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
      flush
      label={`Show ${count} deleted ${noun}`}
      openLabel={`Hide ${count} deleted ${noun}`}
    >
      <p className={cn("text-[13px] text-muted-foreground", panelRowClass)}>
        These rows were removed from Shortlist. Their history still counts in
        the totals above.
      </p>
      {children}
      <div className={panelRowClass}>
      {confirming ? (
        <div
          role="alert"
          className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs"
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
          className="-ml-2 h-7 px-2 text-xs text-muted-foreground hover:text-destructive-text"
          onClick={() => setConfirming(true)}
        >
          <Trash2 className="h-3 w-3" aria-hidden />
          Delete their history
        </Button>
      )}
      </div>
    </Disclosure>
  );
}

function ReportBody({
  report,
  reportWindow,
  onWindowChange,
  updating,
}: {
  report: EffectivenessReport;
  reportWindow: ReportWindow;
  onWindowChange: (next: ReportWindow) => void;
  updating: boolean;
}) {
  const { overall, coverage, runs, requests } = report;

  // The payload echoes its window: a mismatch means this is another window's report standing in
  // while the selected one loads, so dim the figures rather than pass them off as current.
  const dim = cn(
    "transition-opacity motion-reduce:transition-none",
    report.window !== reportWindow && "opacity-60",
  );

  // `since === null` is the "all time" window, which by definition can't be narrower than the data.
  const coversEverything =
    report.first_pick !== null &&
    report.since !== null &&
    new Date(report.first_pick) >= new Date(report.since);

  // ONE panel: the title, the window control (it changes every figure under it), the watch sync,
  // a hairline, then the figures.
  const impact = (body: ReactNode, headless = false) => (
    <section
      aria-labelledby="impact-title"
      data-testid="verdict"
      className="min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-elevated"
    >
      <div
        className={cn(
          "flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-5",
          !headless && "border-b",
        )}
      >
        {/* h2, not h1 — PageHeader above already owns the page's h1 ("Dashboard"), and two of them
            leaves a screen reader with no page title at all. */}
        <h2 id="impact-title" className="text-base font-semibold tracking-tight">
          Impact
        </h2>
        {headless ? (
          <div className="min-w-0 flex-1 basis-80 text-sm text-muted-foreground">{body}</div>
        ) : (
          <>
            <Segmented joined value={reportWindow} onChange={onWindowChange} options={WINDOW_OPTIONS} ariaLabel="Report window" />
            {/* Always mounted so the live region is announced when its text appears, and sized by
                min-width so toggling it never shifts the controls. */}
            <span aria-live="polite" className="min-w-[4.5rem] text-xs text-muted-foreground">
              {updating ? "Updating…" : ""}
            </span>
          </>
        )}
        <span className="md:ml-auto">
          <WatchSyncLine sync={report.watch_sync} />
        </span>
      </div>
      {!headless && (
        <>
          {/* On a young install every window already covers all the data, so the numbers are
              identical whichever button you press — a control that visibly does nothing reads as
              broken. Say why. */}
          {coversEverything && (
            <p className="border-b px-4 py-2 text-[13px] text-muted-foreground sm:px-5">
              Shortlist has only been recording since {formatDate(report.first_pick as string)}, so every window
              covers all of it for now.
            </p>
          )}
          <div className={dim}>{body}</div>
        </>
      )}
    </section>
  );

  if (overall.delivered === 0 && overall.watched === 0) {
    return impact(
      <div className="space-y-3 px-4 py-4 text-sm text-muted-foreground sm:px-5">
        <p>
          {runs.total === 0
            ? "Nothing has reached anyone's rows yet. Build them once from Runs — “Run all rows now” — and this page fills in as people start watching what Shortlist picked."
            : `Nothing reached a row, and nothing was watched, in ${WINDOW_PHRASE[reportWindow]}. Try a longer window.`}
        </p>
        <Button asChild variant="outline" size="sm">
          <Link to="/runs">Open Runs</Link>
        </Button>
      </div>,
    );
  }

  // The morning after the first run: rows are delivered and nobody has watched anything yet, in ANY
  // window (the trend is fixed at 16 weeks, so it is the all-time check). Six half-empty panels
  // would say "nothing" six times; one card says what happens next. `first_pick` is the day
  // counting started.
  const nothingWatchedYet =
    overall.watched === 0 && report.recent.length === 0 && report.trend.every((week) => week.watched === 0);
  if (nothingWatchedYet) {
    return (
      <div className="space-y-4">
        {impact(
          <p>
            Nothing watched yet
            {report.first_pick ? ` — counting started ${formatDate(report.first_pick)}` : ""}.
          </p>,
          true,
        )}
        <WhatHappensNext people={coverage.users_enabled} pendingRequests={requests.pending} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {impact(<Verdict overall={overall} coverage={coverage} reportWindow={reportWindow} />)}

      <div className={cn("space-y-4", dim)}>
        <div className="grid gap-4 lg:grid-cols-2">
          <Section title="Watches per week" hint="Last 16 weeks, regardless of the window above" fill>
            <Trend trend={report.trend} />
          </Section>
          <WhoIsWatching report={report} reportWindow={reportWindow} />
        </div>

        <NeedsALook report={report} reportWindow={reportWindow} />

        {/* The lists that grow get the full width, so their length never strands a card beside them. */}
        {report.top_titles.length > 0 && <MostWatched titles={report.top_titles} reportWindow={reportWindow} />}

        {report.recent.length > 0 && <RecentlyWatched recent={report.recent} />}

        {(requests.sent > 0 || requests.pending > 0) && (
          <RequestsSummary requests={requests} reportWindow={reportWindow} />
        )}
      </div>
    </div>
  );
}

/**
 * What the dashboard has to say before anyone has watched anything: the next run, what fills this
 * page in, and requests waiting on the owner.
 */
function WhatHappensNext({ people, pendingRequests }: { people: number; pendingRequests: number }) {
  const schedule = useSchedule();
  const next = nextRowRun(schedule.data);
  const line = "flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 py-3 sm:px-5";
  return (
    <Section title="What happens next" flush>
      <div className="divide-y divide-border">
        {next && (
          <div className={line}>
            <span>
              The next run builds rows for {people} {people === 1 ? "person" : "people"}
            </span>
            <span className="text-sm text-muted-foreground">
              {dayTime(next.at)} · {timeUntil(next.at)}
            </span>
          </div>
        )}
        <div className={line}>
          <span>Watches show up here as people watch their rows</span>
        </div>
        {pendingRequests > 0 && (
          <div className={line}>
            <span>Requests waiting for approval</span>
            <span className="text-sm">
              <span className="font-semibold text-warning">{pendingRequests}</span>
              <span className="text-muted-foreground"> · </span>
              <Link to="/requests" className="text-foreground underline-offset-4 hover:underline">
                Review →
              </Link>
            </span>
          </div>
        )}
      </div>
    </Section>
  );
}

/** Sent, watched since, and waiting on you — each figure in its own tile, so none can sit in another's slot. */
function RequestsSummary({ requests, reportWindow }: { requests: EffectivenessReport["requests"]; reportWindow: ReportWindow }) {
  return (
    <section
      aria-label="Requests"
      className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-1 rounded-xl border bg-card px-4 py-3.5 shadow-elevated sm:px-5"
    >
      <h2 className="text-base font-semibold tracking-tight">Requests</h2>
      <p className="text-sm text-muted-foreground">
        {/* Only the figures that are not zero: "0 sent · 0 watched since" is a row of nothing. */}
        {requests.sent > 0 && (
          <>
            <span data-testid="requests-sent">{requests.sent}</span> sent
            {requests.watched_after_sent > 0 && (
              <>
                {" · "}
                {requests.watched_after_sent} watched since
                {/* App-neutral on purpose — see run-stat-tiles: the route is a setting this line cannot see. */}
                <span className="sr-only"> in {WINDOW_PHRASE[reportWindow]}</span>
              </>
            )}
            {" · "}
          </>
        )}
        {requests.pending > 0 && (
          <>
            <span className="font-semibold text-warning">{requests.pending} awaiting approval</span>
            {" · "}
            <Link to="/requests" className="text-foreground underline-offset-4 hover:underline">
              Review →
            </Link>
          </>
        )}
        {requests.sent > 0 && (
          <>
            {requests.pending > 0 && " · "}
            <Link to="/requests?tab=sent" className="text-foreground underline-offset-4 hover:underline">
              Send log →
            </Link>
          </>
        )}
      </p>
    </section>
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
          // `relative` so the tile's sr-only text stays inside the scrolling shelf: absolutely
          // positioned with no positioned ancestor, it escapes the clip and widens the page.
          <li key={`${t.tmdb_id}-${t.media_type}`} className="relative grid w-[128px] shrink-0 content-start gap-1.5 sm:w-auto">
            <div className="relative">
              <TitlePoster
                ratingKey={t.rating_key}
                className="aspect-[2/3] h-auto w-full rounded-md sm:h-auto sm:w-full"
              />
              <span className="absolute left-1.5 top-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded bg-background/80 px-1 text-xs font-semibold tabular-nums text-muted-foreground ring-1 ring-border-strong">
                {i + 1}
              </span>
            </div>
            <p className="truncate text-sm font-medium text-foreground" title={t.title}>
              {t.title}
            </p>
            {/* The faces say who; the count is in words for a screen reader and on hover. The
                TMDB/IMDb/Trakt logos are gone from the tile — three coloured marks per poster were
                the loudest thing on the page. */}
            <div className="flex items-center" title={`${t.watchers} ${t.watchers === 1 ? "watcher" : "watchers"}`}>
              {t.watcher_sample.length > 0 && (
                <span className="flex -space-x-1">
                  {t.watcher_sample.map((w) => (
                    <UserAvatar key={w.id} name={w.name} size="xs" labelled className="ring-2 ring-card" />
                  ))}
                </span>
              )}
              <span className="sr-only">
                {t.watchers} {t.watchers === 1 ? "watcher" : "watchers"}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** How many watches show before "See all". The server sends at most 20
 *  (`report_service._recent_watches`), so the list is bounded twice over; the fold is about what is
 *  worth reading at a glance, not about volume. */
const RECENT_SHOWN = 5;

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

const VERB_BADGE: Record<string, string> = {
  finished: "bg-success/15 text-success",
  started: "bg-secondary text-secondary-foreground",
  watched: "bg-secondary text-secondary-foreground",
};

/**
 * The newest watches, newest first, filed under their day.
 *
 * Each line used to be one run of text — person, verb, title, row, time — so the title, which is the
 * news, sat in the middle of a sentence. It now leads with the poster and title, says finished /
 * started / watched as a badge, puts who and which row on the line under it, and keeps the time and
 * the look-up links at the end. The time is "3d ago", newest first.
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
            <span className={cn("rounded-full px-2 text-xs font-semibold capitalize", VERB_BADGE[verb])}>
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
          </div>
      </li>
    );
  };

  const [all, setAll] = useState(false);
  const shown = all ? recent : recent.slice(0, RECENT_SHOWN);

  return (
    <Section
      title="Recently watched"
      hint={`The ${recent.length === 1 ? "newest watch" : `newest ${recent.length} watches`}. Older ones are on each person's page.`}
      actions={
        // No page lists every watch, so "See all" opens the rest here rather than linking away.
        recent.length > RECENT_SHOWN ? (
          <button
            type="button"
            onClick={() => setAll((v) => !v)}
            aria-expanded={all}
            className="rounded-sm text-[13px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {all ? "Show fewer" : `See all ${recent.length} →`}
          </button>
        ) : undefined
      }
      flush
    >
      <ul aria-label="Recently watched from Shortlist" className="divide-y divide-border px-4 sm:px-5">
        {shown.map(line)}
      </ul>
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
      skeleton={<ReportSkeleton />}
    >
      {(data) => (
        <ReportBody
          report={data}
          reportWindow={reportWindow}
          onWindowChange={setReportWindow}
          updating={report.isFetching}
        />
      )}
    </QueryBoundary>
  );
}
