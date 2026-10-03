import type { UseQueryResult } from "@tanstack/react-query";
import { Clock } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router";

import { StatusCell, StatusRow, StatusStrip } from "@/components/status-strip";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { describeCron } from "@/lib/cron";
import {
  formatDuration,
  runElapsedMs,
  runStatusLabel,
  timeAgo,
  timeUntil,
} from "@/lib/format";
import { latestFinishedRun, nextRowRun } from "@/lib/dashboard-status";
import { cannotHide, privacyGlance, rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { hasPrivacyWarning } from "@/lib/run-privacy";
import type {
  AccountPrivacy,
  EffectivenessReport,
  PrivacyStatus,
  Run,
  ScheduleResponse,
  User,
} from "@/lib/types";
import { dayTime } from "@/lib/when";

const linkClass =
  "rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Pending() {
  return <Skeleton className="h-6 w-24" />;
}

function LastRunCell({
  report,
  runs,
  privacy,
}: {
  report: UseQueryResult<EffectivenessReport>;
  runs: UseQueryResult<Run[]>;
  privacy: PrivacyStatus | undefined;
}) {
  const testId = "status-last-run";
  if (runs.isPending && report.isPending) {
    return <StatusCell testId={testId} label="Last run" tone="neutral" value={<Pending />} />;
  }
  const run = latestFinishedRun(runs.data);
  if (run) {
    const warned = hasPrivacyWarning(run);
    const elapsed = runElapsedMs(run.began_at, run.finished_at);
    const people = run.stats.users_ok ?? 0;
    // People the run could not build for: the three tiers the bell draws (`_last_run_problem`) — a
    // failed run, an OK run that failed someone (amber), a clean one.
    const failed = run.stats.users_error ?? 0;
    return (
      <StatusCell
        testId={testId}
        label="Last run"
        tone={run.status === "error" ? "error" : warned || failed > 0 ? "warn" : "ok"}
        value={
          // The run page's words for the same state ("OK with warnings"), so the link below never
          // lands on a different name for what this cell just said.
          warned ? (
            <Badge variant="warning" className="border-warning/40">
              OK with warnings
            </Badge>
          ) : (
            runStatusLabel(run.status)
          )
        }
        sub={
          <>
            <Link to={`/runs/${run.id}`} className={linkClass}>
              {run.finished_at ? `${dayTime(run.finished_at)} · ${timeAgo(run.finished_at)}` : `Run #${run.id}`}
            </Link>
            {/* A dry run wrote nothing, so its "OK" is not a claim about anyone's rows. */}
            {run.dry_run && " · dry run"}
            {` · ${people} ${people === 1 ? "person" : "people"}`}
            {failed > 0 && ` · ${failed} failed`}
            {elapsed !== null && ` · ${formatDuration(elapsed)}`}
          </>
        }
      />
    );
  }
  // The runs list may not reach back to it (cleared history keeps the report's own record); the
  // report still knows the status, just not this run's privacy, so it says the status alone.
  const status = report.data?.runs.last_status;
  if (status) {
    const finished = report.data?.runs.last_finished ?? null;
    return (
      <StatusCell
        testId={testId}
        label="Last run"
        tone={status === "error" ? "error" : status === "ok" ? "ok" : "neutral"}
        value={runStatusLabel(status)}
        sub={finished ? `${dayTime(finished)} · ${timeAgo(finished)}` : undefined}
      />
    );
  }
  return (
    <StatusCell
      testId={testId}
      label="Last run"
      tone="neutral"
      value="None yet"
      sub={privacy && privacy.rows_on_plex.length === 0 ? "No rows on Plex so far" : "No finished run recorded"}
    />
  );
}

function NextRunCell({
  schedule,
  users,
}: {
  schedule: UseQueryResult<ScheduleResponse>;
  users: User[] | undefined;
}) {
  const testId = "status-next-run";
  if (schedule.isPending) {
    return <StatusCell testId={testId} icon={Clock} label="Next run" value={<Pending />} />;
  }
  if (schedule.isError) {
    return (
      <StatusCell testId={testId} icon={Clock} label="Next run" value="Unknown" sub="Couldn’t read the schedule" />
    );
  }
  const next = nextRowRun(schedule.data);
  const enabled = users?.filter((user) => user.enabled && !user.departed).length;
  const who = enabled === undefined ? null : `${enabled} ${enabled === 1 ? "person" : "people"} enabled`;
  if (!next) {
    return (
      <StatusCell
        testId={testId}
        icon={Clock}
        label="Next run"
        value="Not scheduled"
        sub={who ? `No row has a schedule · ${who}` : "No row has a schedule"}
      />
    );
  }
  return (
    <StatusCell
      testId={testId}
      icon={Clock}
      label="Next run"
      value={dayTime(next.at)}
      sub={[timeUntil(next.at), who].filter(Boolean).join(" · ")}
      title={describeCron(next.cron) || undefined}
    />
  );
}

/** "kid can see 3 rows that aren't theirs" — the account's own evidence, never a bare "not private".
 *  The same figure and words as the Users list and the Privacy page's "Hides X of Y rows". */
function exposedPhrase(account: AccountPrivacy, status: PrivacyStatus, notEnforced: boolean): string {
  const name = account.display_name || account.user;
  const exposed = rowsNotHidden(account, status);
  if (exposed > 0) return `${name} ${rowsNotTheirs(exposed)}`;
  if (notEnforced) return `Plex isn’t applying ${name}’s hide rules`;
  return `${name} isn’t hiding every row`;
}

function PrivacyCell({ privacy }: { privacy: UseQueryResult<PrivacyStatus> }) {
  const testId = "status-privacy";
  const cell = (tone: "ok" | "warn" | "error" | "neutral", value: ReactNode, sub?: ReactNode) => (
    <StatusCell testId={testId} label="Privacy" tone={tone} value={value} sub={sub} />
  );
  const toPrivacy = (text: string) => (
    <Link to="/privacy" className={linkClass}>
      {text} →
    </Link>
  );
  if (privacy.isPending) return cell("neutral", <Pending />);
  if (privacy.isError) return cell("neutral", "Couldn’t check", toPrivacy("Open Privacy"));
  const glance = privacyGlance(privacy.data);
  switch (glance.kind) {
    case "unreadable":
      return cell("warn", "Couldn’t read plex.tv", toPrivacy("Open Privacy"));
    case "rows_unknown":
      return cell("neutral", "Not measured", "Couldn’t list the rows on Plex");
    case "nothing_to_hide": {
      // "Every row is hidden before it appears" is untrue of an account Plex refuses hide rules for,
      // the moment its first row lands — so that promise is made only when no such account exists.
      const stuck = cannotHide(privacy.data).length;
      if (stuck > 0) {
        return cell(
          "warn",
          "Nothing to hide yet",
          toPrivacy(`${stuck} ${stuck === 1 ? "account" : "accounts"} can’t be hidden`),
        );
      }
      return cell("neutral", "Nothing to hide yet", "Every row is hidden before it appears");
    }
    case "no_accounts":
      return cell("neutral", "No one to hide from", "Only the owner’s account, or accounts you left alone");
    case "counted": {
      const first = glance.exposed[0];
      const enforcement = privacy.data.enforcement;
      return cell(
        glance.hiding === glance.total ? "ok" : "warn",
        `${glance.hiding} of ${glance.total} hide every row`,
        first
          ? toPrivacy(
              exposedPhrase(
                first,
                privacy.data,
                Boolean(enforcement?.measured && (enforcement.not_enforced?.[first.user]?.length ?? 0) > 0),
              ),
            )
          : `Read from plex.tv ${timeAgo(privacy.data.read_at)}`,
      );
    }
  }
}

/** Whether Plex answered, judged from the privacy reading the strip already has — it needs plex.tv
 *  (the share filters) and the server (the rows), so a clean reading proves both were reachable
 *  without a request of its own. */
function PlexCell({ privacy }: { privacy: UseQueryResult<PrivacyStatus> }) {
  const testId = "status-plex";
  if (privacy.isPending) return <StatusCell testId={testId} label="Plex" tone="neutral" value={<Pending />} />;
  if (privacy.isError) {
    return <StatusCell testId={testId} label="Plex" tone="neutral" value="Unknown" sub="Couldn’t run the check" />;
  }
  if (privacy.data.error) {
    return (
      <StatusCell
        testId={testId}
        label="Plex"
        tone="error"
        value="plex.tv unreachable"
        sub="Sharing settings couldn’t be read"
      />
    );
  }
  if (privacy.data.rows_error) {
    return (
      <StatusCell
        testId={testId}
        label="Plex"
        tone="error"
        value="Server not answering"
        sub="Couldn’t read its rows"
      />
    );
  }
  return (
    <StatusCell
      testId={testId}
      label="Plex"
      tone="ok"
      value="Connected"
      sub={`Server and plex.tv read ${timeAgo(privacy.data.read_at)}`}
    />
  );
}

/**
 * The four facts the dashboard opens on: did last night work, when is the next one, is every row
 * still private, is Plex there. Each one restates an endpoint's answer and adds no judgement.
 */
export function DashboardStatus({
  report,
  runs,
  privacy,
  schedule,
  users,
}: {
  report: UseQueryResult<EffectivenessReport>;
  runs: UseQueryResult<Run[]>;
  privacy: UseQueryResult<PrivacyStatus>;
  schedule: UseQueryResult<ScheduleResponse>;
  users: User[] | undefined;
}) {
  return (
    <StatusStrip label="Status">
      <StatusRow className="lg:grid-cols-4">
        <LastRunCell report={report} runs={runs} privacy={privacy.data} />
        <NextRunCell schedule={schedule} users={users} />
        <PrivacyCell privacy={privacy} />
        <PlexCell privacy={privacy} />
      </StatusRow>
    </StatusStrip>
  );
}
