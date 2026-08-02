import {
  CalendarClock,
  Clock,
  RefreshCw,
  Send,
  Trash2,
  TrendingUp,
  Users as UsersIcon,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { QueryBoundary } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { StatTile } from "@/components/stat-tile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, formatHitRate, timeAgo } from "@/lib/format";
import {
  useClearDeletedRows,
  useDeletedRows,
  useReport,
  useSyncWatched,
} from "@/lib/queries";
import type { EffectivenessReport, ReportWindow } from "@/lib/types";

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
        {sync.next && ` · next check ${formatDate(sync.next)}`}. It also
        refreshes on every run.
      </span>
      <div className="flex items-center gap-2">
        {syncNow.isError && (
          <span role="alert" className="text-destructive">
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

/** A tiny watches-per-week bar chart — no library, just normalized divs. */
function Trend({ trend }: { trend: EffectivenessReport["trend"] }) {
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
  return (
    <div>
      {/* The bars are aria-hidden (a per-bar `title` only reaches a mouse), so a screen reader gets
          NOTHING from this chart without a text alternative — this is that alternative. */}
      <p className="sr-only">
        {total} watched across the last {trend.length} weeks
        {first && last
          ? `, from ${first.watched} in week ${first.week} to ${last.watched} in week ${last.week}`
          : ""}
        .
      </p>
      <div className="flex h-20 items-end gap-1" aria-hidden="true">
        {trend.map((t) => (
          <div
            key={t.week}
            className="flex-1 rounded-t bg-primary/70"
            style={{ height: `${Math.max(4, (t.watched / max) * 100)}%` }}
            title={`${t.week}: ${t.watched} watched`}
          />
        ))}
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
  delivered,
  max,
}: {
  watched: number;
  delivered: number;
  max: number;
}) {
  return (
    // Label FIRST, bar last. The label sizes itself and never wraps; the bar is the fixed-width
    // element, so it is the bars' right edges that line up down the list. Sizing the label instead
    // meant picking a width — and any width is wrong for some count: w-32 wrapped "3 watched · 103
    // delivered" onto two lines, and even w-44 overflows once a row passes 999 watched or 9999
    // delivered. This way no count can break the layout.
    <div className="flex min-w-0 items-center gap-2">
      {/* Two labelled numbers, NOT "{watched} of {delivered}". They are counts over two different
          sets — watched-in-window and delivered-in-window — so presenting them as a fraction makes
          "4 of 0" reachable whenever delivery paused (a weekly row cron on a 7-day window). That is
          the same misleading fraction this rewrite exists to remove. */}
      <span className="shrink-0 whitespace-nowrap text-right tabular-nums text-muted-foreground">
        <span className="font-medium text-foreground">{watched}</span> watched
        {/* "delivered", not "sent" — the Requests card on this same page uses "sent" to mean asked of
            Sonarr/Radarr, and two meanings of the word side by side is exactly the kind of quiet
            ambiguity this rewrite is meant to remove. */}
        {delivered > 0 && ` · ${delivered} delivered`}
      </span>
      <div className="h-1.5 w-24 shrink-0 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary"
          style={{ width: `${max > 0 ? (watched / max) * 100 : 0}%` }}
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
    <Card>
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
  children,
}: {
  count: number;
  noun: string;
  children: React.ReactNode;
}) {
  if (count === 0) return null;
  return (
    <Disclosure label={`${count} ${noun} with none in this window`}>
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
      className="flex items-center justify-between gap-3 text-sm"
    >
      <span className="truncate">{p.display_name || p.username}</span>
      <CountBar watched={p.watched} delivered={p.delivered} max={max} />
    </div>
  );

  return (
    <Section
      title="By person"
      hint={`Picks watched in ${WINDOW_PHRASE[reportWindow]}, of picks delivered.`}
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
          {overflow.length > 0 && (
            <Disclosure
              label={`${overflow.length} more ${overflow.length === 1 ? "person" : "people"} watched something`}
            >
              {overflow.map(line)}
            </Disclosure>
          )}
          <ZeroDisclosure count={idle.length} noun="people">
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
      className="flex items-center justify-between gap-3 text-sm"
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <span
          className={`truncate ${r.deleted ? "text-muted-foreground" : ""}`}
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
      <CountBar watched={r.watched} delivered={r.delivered} max={max} />
    </div>
  );

  return (
    <Section
      title="By row"
      hint={`Picks watched in ${WINDOW_PHRASE[reportWindow]}, of picks delivered.`}
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
          className="mt-1 h-7 px-2 text-xs text-muted-foreground hover:text-destructive"
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
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          icon={TrendingUp}
          label="Watched"
          value={overall.watched}
          hint={
            <Delta value={overall.watched_delta} reportWindow={reportWindow} />
          }
          title="Picks people watched in this window. A pick delivered earlier and watched now counts here — this is about watching, not delivery."
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
          hint="The long view — always the last 16 weeks, whatever window is selected."
        >
          <Trend trend={report.trend} />
        </Section>

        <Section
          title="Picks that get watched"
          hint={`Of the picks delivered, the share watched within ${landing.matured_days} days.`}
        >
          {landing.rate === null ? (
            <p className="text-sm text-muted-foreground">
              Not enough time has passed. A pick only counts as watched within{" "}
              {landing.matured_days} days of delivery, so this needs picks
              delivered at least {landing.matured_days} days ago — try a longer
              window.
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
              <p className="text-xs text-muted-foreground/80">
                Measured only over picks old enough to have had their full{" "}
                {landing.matured_days} days — a pick delivered yesterday can’t
                have been watched “within 30 days” yet, so counting it would
                drag this toward zero for no reason.
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
            hint={`The picks the most people watched in ${WINDOW_PHRASE[reportWindow]}.`}
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

      {report.recent.length > 0 && (
        <Section title="Recently watched from Shortlist">
          <ul className="space-y-1 text-sm">
            {report.recent.slice(0, 12).map((w, i) => (
              <li
                // watched_at (when present) is a stable, unique-enough identity for this list;
                // falling back to the index only for the rare entry missing it.
                key={`${w.username}-${w.title}-${w.watched_at ?? i}`}
                className="flex flex-wrap items-baseline gap-x-2 text-muted-foreground"
              >
                <span className="font-medium text-foreground">
                  {w.display_name || w.username}
                </span>
                watched
                <span className="text-foreground">{w.title}</span>
                <Badge variant="secondary" className="font-normal">
                  {w.row}
                </Badge>
                {w.watched_at && <span>· {timeAgo(w.watched_at)}</span>}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

/**
 * The dashboard tracking report — what got watched, by whom, from which row, over a chosen window.
 *
 * Windowed on purpose. Every figure here used to be lifetime-cumulative, which made each ratio a
 * measure of how long Shortlist had been installed rather than of how good the picks were: a pick
 * can only be credited within 30 days of delivery, but the old denominator kept every pick ever
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
