import {
  CalendarClock,
  Clock,
  Play,
  RefreshCw,
  Send,
  Target,
  TrendingUp,
  Users as UsersIcon,
} from "lucide-react";
import { Link } from "react-router-dom";

import { QueryBoundary } from "@/components/query-boundary";
import { StatTile } from "@/components/stat-tile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate, timeAgo } from "@/lib/format";
import { useReport, useSyncWatched } from "@/lib/queries";
import type { EffectivenessReport } from "@/lib/types";

/** Shows when the daily watch-status sync last ran and next fires, with a manual "Sync now". */
function WatchSyncLine({ sync }: { sync: EffectivenessReport["watch_sync"] }) {
  const syncNow = useSyncWatched();
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span>
        Watch status{" "}
        {sync.last ? `synced ${timeAgo(sync.last)}` : "not synced yet"}
        {sync.next && ` · next check ${formatDate(sync.next)}`}. It also
        refreshes on every run.
      </span>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending || syncNow.isSuccess}
      >
        <RefreshCw aria-hidden="true" />
        {syncNow.isSuccess ? "Syncing…" : "Sync now"}
      </Button>
    </div>
  );
}

function pct(rate: number | null): string {
  return rate === null ? "—" : `${Math.round(rate * 100)}%`;
}

/** A tiny watches-per-week bar chart — no library, just normalized divs. */
function Trend({ trend }: { trend: EffectivenessReport["trend"] }) {
  const recent = trend.slice(-16);
  const max = Math.max(1, ...recent.map((t) => t.watched));
  if (recent.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        No watches recorded yet — this fills in as people watch their picks.
      </p>
    );
  return (
    <div className="flex h-20 items-end gap-1" aria-hidden="true">
      {recent.map((t) => (
        <div
          key={t.week}
          className="flex-1 rounded-t bg-primary/70"
          style={{ height: `${Math.max(4, (t.watched / max) * 100)}%` }}
          title={`${t.week}: ${t.watched} watched`}
        />
      ))}
    </div>
  );
}

function HitBar({
  delivered,
  watched,
  hit_rate,
}: {
  delivered: number;
  watched: number;
  hit_rate: number | null;
}) {
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-muted">
        <div
          className="h-full rounded-full bg-primary"
          style={{ width: `${Math.round((hit_rate ?? 0) * 100)}%` }}
        />
      </div>
      <span className="tabular-nums text-muted-foreground">
        {pct(hit_rate)}{" "}
        <span className="text-xs">
          ({watched}/{delivered})
        </span>
      </span>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
        {children}
      </CardContent>
    </Card>
  );
}

function ReportBody({ report }: { report: EffectivenessReport }) {
  const { overall, coverage, runs, requests } = report;

  if (overall.delivered === 0) {
    return (
      <Card>
        <CardContent className="pt-6 text-sm text-muted-foreground">
          No picks delivered yet — run Shortlist, and once people start watching
          what it picked, the tracking shows up here.
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      {/* Headline metrics */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <StatTile
          icon={Target}
          label="Hit rate"
          value={pct(overall.hit_rate)}
          hint="picks watched"
        />
        <StatTile
          icon={Play}
          label="Watched"
          value={overall.watched}
          hint={`of ${overall.delivered} delivered`}
        />
        <StatTile
          icon={TrendingUp}
          label="Watched (7d)"
          value={overall.watched_last_7d}
          hint="last 7 days"
        />
        <StatTile
          icon={Clock}
          label="Avg to watch"
          value={
            overall.avg_days_to_watch === null
              ? "—"
              : `${overall.avg_days_to_watch}d`
          }
          hint="delivery → watch"
        />
        <StatTile
          icon={UsersIcon}
          label="Reach"
          value={coverage.users_with_picks}
          hint={`of ${coverage.users_enabled} enabled`}
        />
        <StatTile
          icon={CalendarClock}
          label="Runs"
          value={runs.total}
          hint={runs.last_finished ? timeAgo(runs.last_finished) : "never"}
          tone={runs.errors_last ? "destructive" : "default"}
        />
      </div>

      <WatchSyncLine sync={report.watch_sync} />

      <Section title="Watches per week">
        <Trend trend={report.trend} />
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="By person">
          <div className="space-y-1.5">
            {report.per_user.slice(0, 10).map((u) => (
              <div
                key={u.slug}
                className="flex items-center justify-between gap-3 text-sm"
              >
                <span className="truncate">{u.username}</span>
                <HitBar {...u} />
              </div>
            ))}
          </div>
        </Section>

        <Section title="By row">
          <div className="space-y-1.5">
            {report.per_row.map((r) => (
              <div
                key={`${r.slug}-${r.section_key}-${r.library}`}
                className="flex items-center justify-between gap-3 text-sm"
              >
                <span className="flex min-w-0 items-center gap-1.5">
                  <span className="truncate">{r.name}</span>
                  {/* A row across >1 library is one collection per library. A {library_name} name
                      already reads "✨ Movies …"; otherwise tag which library this line is. */}
                  {r.library && !r.name.includes(r.library) && (
                    <Badge variant="secondary" className="shrink-0 font-normal">
                      {r.library}
                    </Badge>
                  )}
                </span>
                <HitBar {...r} />
              </div>
            ))}
          </div>
        </Section>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        {report.top_titles.length > 0 && (
          <Section title="Landing best">
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
          <Section title="Requests">
            <div className="flex items-center gap-2 text-sm">
              <Send className="h-4 w-4 text-muted-foreground" aria-hidden />
              <span>
                <span className="font-medium text-foreground">
                  {requests.sent}
                </span>{" "}
                sent to Sonarr/Radarr ·{" "}
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
                key={`${w.username}-${w.title}-${i}`}
                className="flex flex-wrap items-baseline gap-x-2 text-muted-foreground"
              >
                <span className="font-medium text-foreground">
                  {w.username}
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

/** The dashboard tracking report — delivered-vs-watched, reach, momentum, top titles, requests, and a
 * recent-watches feed. All from picks.watched_at. */
export function ImpactReport() {
  const report = useReport();
  return (
    <QueryBoundary
      query={report}
      skeleton={<Skeleton className="h-96 w-full" />}
    >
      {(data) => <ReportBody report={data} />}
    </QueryBoundary>
  );
}
