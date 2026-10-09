import { Link, useNavigate } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { describeCron } from "@/lib/cron";
import { latestFinishedRun, nextRowRun } from "@/lib/dashboard-status";
import { plural, runStatusLabel, timeAgo, timeUntil } from "@/lib/format";
import { useRecentRuns, useSchedule, useStartRun, useUsers } from "@/lib/queries";
import { hasPrivacyWarning } from "@/lib/run-privacy";

/**
 * The nightly rows run, first on the Jobs tab: it is the product's core job, and the upkeep jobs
 * below only keep it tidy. Everything here is read from the schedule, the roster and the last
 * finished run; "Run now" is the dashboard's own run, opened on arrival.
 */
export function NightlyRunCard() {
  const schedule = useSchedule();
  const users = useUsers();
  const runs = useRecentRuns();
  const startRun = useStartRun();
  const navigate = useNavigate();

  const next = nextRowRun(schedule.data);
  const scheduledRows = (schedule.data?.rows ?? []).filter((group) => group.cron).reduce((n, group) => n + group.rows.length, 0);
  const people = users.data?.filter((user) => user.enabled && !user.departed).length;
  const last = latestFinishedRun(runs.data);
  const warned = last ? hasPrivacyWarning(last) : false;
  const failed = last?.status === "error";

  const plan = schedule.isPending
    ? null
    : schedule.isError
      ? "Couldn’t read the schedule"
      : [
          next ? `${describeCron(next.cron) || next.cron} · next ${timeUntil(next.at)}` : "No row has a schedule",
          scheduledRows > 0 && `${plural(scheduledRows, "row")}${people === undefined ? "" : ` for ${people} ${people === 1 ? "person" : "people"}`}`,
        ]
          .filter(Boolean)
          .join(" · ");

  return (
    <Card>
      <CardContent className="space-y-3 pt-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 space-y-1">
            <h2 className="text-xl font-semibold">Nightly rows run</h2>
            {plan === null ? (
              <Skeleton className="h-5 w-72" />
            ) : (
              <p className="text-sm text-muted-foreground">{plan}</p>
            )}
            {last && (
              <p className="flex flex-wrap items-center gap-2 pt-1 text-sm">
                Last result
                <Badge variant={failed ? "destructive" : warned ? "warning" : "success"}>
                  {warned ? "OK with warnings" : runStatusLabel(last.status)}
                </Badge>
                <Link
                  to={`/runs/${last.id}`}
                  className="text-muted-foreground underline underline-offset-2 hover:text-foreground"
                >
                  {last.finished_at ? timeAgo(last.finished_at) : `Run #${last.id}`}
                </Link>
              </p>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild variant="outline">
              <Link to="/rows">Change schedule</Link>
            </Button>
            <Button
              loading={startRun.isPending}
              onClick={() => startRun.mutate({}, { onSuccess: (created) => navigate(`/runs/${created.run_id}`) })}
            >
              Run now
            </Button>
          </div>
        </div>
        {startRun.isError && (
          <MutationAlert
            error={startRun.error}
            fallback="Couldn’t start that run. Check the server log and try again."
          />
        )}
      </CardContent>
    </Card>
  );
}
