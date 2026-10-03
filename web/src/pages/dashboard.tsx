import { FlaskConical, Play } from "lucide-react";
import { useNavigate } from "react-router";

import { DashboardStatus } from "@/components/dashboard/dashboard-status";
import { FirstRunPanel } from "@/components/dashboard/first-run-panel";
import { ImpactReport } from "@/components/dashboard/impact-report";
import { PrivacyCallout } from "@/components/dashboard/privacy-callout";
import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { latestFinishedRun, nextRowRun } from "@/lib/dashboard-status";
import { usePrivacyGlance } from "@/lib/privacy-attention";
import {
  useRecentRuns,
  useReport,
  useSchedule,
  useStartRun,
  useUsers,
} from "@/lib/queries";

/**
 * The dashboard opens on last night's run and whether every row is still private, then the Impact
 * report: what Shortlist put in people's rows and how much of it they watched.
 *
 * Before anything has run AND nothing has been delivered, it is one panel that builds everyone's rows
 * instead — an empty report there only said "nothing yet" in six places. Both conditions, because
 * clearing run history keeps every delivered pick (and the report built from them): a server with
 * rows on Plex is not a first run just because its history was emptied.
 *
 * "Is anything wrong" is still the notification bell's job; the strip states facts, not alerts.
 */
export function DashboardPage() {
  const report = useReport();
  const runs = useRecentRuns();
  const privacy = usePrivacyGlance();
  const schedule = useSchedule();
  const users = useUsers();
  const startRun = useStartRun();
  const navigate = useNavigate();

  const firstRun =
    report.data !== undefined && report.data.runs.last_finished === null && report.data.first_pick === null;
  const pending = startRun.isPending ? (startRun.variables?.dry_run ? "dry" : "run") : null;
  // The same mutation the Runs page uses, then straight to the run — the Rows page's Run button does
  // the same, so pressing it from here never leaves the owner wondering whether anything started.
  const start = (dryRun: boolean) =>
    startRun.mutate(dryRun ? { dry_run: true } : {}, {
      onSuccess: (created) => navigate(`/runs/${created.run_id}`),
    });
  const enabled = (users.data ?? []).filter((user) => user.enabled && !user.departed);

  return (
    <div className="space-y-6">
      <PageHeader
        className="mb-0"
        title="Dashboard"
        subtitle="Last night’s run, whether every row is still private, and what got watched."
        actions={
          // The first-run panel carries these itself; one filled-amber button per screen.
          report.data !== undefined &&
          !firstRun && (
            <>
              <Button variant="outline" onClick={() => start(true)} loading={pending === "dry"} disabled={pending !== null}>
                {pending !== "dry" && <FlaskConical aria-hidden="true" />}
                Dry run
              </Button>
              <Button onClick={() => start(false)} loading={pending === "run"} disabled={pending !== null}>
                {pending !== "run" && <Play aria-hidden="true" />}
                Run now
              </Button>
            </>
          )
        }
      />

      {/* A refused or failed start says why in plain English (PMS too old, Plex unreachable). */}
      {startRun.isError && (
        <MutationAlert
          error={startRun.error}
          fallback="Couldn’t start that run. Check the server log and try again."
        />
      )}

      <DashboardStatus
        report={report}
        runs={runs}
        privacy={privacy}
        schedule={schedule}
        users={users.data}
      />

      <PrivacyCallout status={privacy.data} lastRun={latestFinishedRun(runs.data)} />

      {report.isPending ? (
        <Skeleton className="h-96 w-full" />
      ) : firstRun ? (
        <FirstRunPanel
          people={enabled}
          nextRun={nextRowRun(schedule.data)?.at}
          pending={pending}
          onRun={start}
        />
      ) : (
        <ImpactReport />
      )}
    </div>
  );
}
