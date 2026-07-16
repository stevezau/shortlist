import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Gauge,
  Search,
  ShieldAlert,
  ShieldCheck,
  ShieldQuestion,
  Target,
  Users as UsersIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { StatTile } from "@/components/stat-tile";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { UserCard } from "@/components/user-card";
import { apiErrorMessage } from "@/lib/api";
import { dashboardStats } from "@/lib/dashboard-stats";
import { settingString, timeFromCron } from "@/lib/format";
import {
  queryKeys,
  usePatchUser,
  usePrivacyStatus,
  useRunPrivacyCheck,
  useRuns,
  useSettings,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { PrivacyStatus, User } from "@/lib/types";

function PrivacyBadge({ status }: { status: PrivacyStatus | undefined }) {
  if (!status || status.passed === null) {
    return (
      <Badge variant="warning">
        <ShieldQuestion className="h-3 w-3" aria-hidden="true" />
        Not checked yet
      </Badge>
    );
  }
  if (status.passed) {
    return (
      <Badge variant="success">
        <ShieldCheck className="h-3 w-3" aria-hidden="true" />
        Private
        {status.last_check
          ? ` · ${new Date(status.last_check).toLocaleDateString()}`
          : ""}
      </Badge>
    );
  }
  return (
    <Badge variant="destructive">
      <ShieldAlert className="h-3 w-3" aria-hidden="true" />
      Check failed
    </Badge>
  );
}

function DashboardSkeleton() {
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {Array.from({ length: 6 }, (_, i) => (
        <Card key={i}>
          <CardHeader>
            <Skeleton className="h-5 w-24" />
          </CardHeader>
          <CardContent className="space-y-3">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-4 w-40" />
            <Skeleton className="h-8 w-full" />
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

export function DashboardPage() {
  const usersQuery = useUsers();
  const runsQuery = useRuns();
  const privacyQuery = usePrivacyStatus();
  const settingsQuery = useSettings();
  const startRun = useStartRun();
  const patchUser = usePatchUser();
  const privacyCheck = useRunPrivacyCheck();
  const queryClient = useQueryClient();

  const [search, setSearch] = useState("");
  // user identifier (slug) → current pipeline stage, fed by SSE during runs
  const [activeStages, setActiveStages] = useState<Record<string, string>>({});
  const [pendingRunUserIds, setPendingRunUserIds] = useState<Set<number>>(
    new Set(),
  );

  useSSE({
    onRunUserStage: (event) => {
      setActiveStages((stages) => ({ ...stages, [event.user]: event.stage }));
    },
    onRunFinished: () => {
      setActiveStages({});
      setPendingRunUserIds(new Set());
      void queryClient.invalidateQueries({ queryKey: queryKeys.users });
      void queryClient.invalidateQueries({ queryKey: queryKeys.runs });
    },
    onPrivacyStatus: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.privacy });
    },
  });

  const scheduleCron = settingsQuery.data
    ? settingString(settingsQuery.data, "schedule.cron")
    : "";
  const schedule = scheduleCron ? timeFromCron(scheduleCron) : null;
  // Name the cadence honestly — a weekly schedule isn't "tonight" (it runs Sundays).
  const scheduleSubtitle = schedule
    ? schedule.weekly
      ? `Rows refresh every Sunday at ${schedule.time}`
      : `Rows refresh nightly at ${schedule.time}`
    : "No schedule set yet";

  const handleRunNow = (user: User) => {
    setPendingRunUserIds((ids) => new Set(ids).add(user.id));
    startRun.mutate(
      { user_ids: [user.id] },
      {
        onError: () =>
          setPendingRunUserIds((ids) => {
            const next = new Set(ids);
            next.delete(user.id);
            return next;
          }),
      },
    );
  };

  const filteredUsers = useMemo(() => {
    const users = usersQuery.data ?? [];
    const needle = search.trim().toLowerCase();
    const matched = needle
      ? users.filter((user) => user.username.toLowerCase().includes(needle))
      : users;
    return [...matched].sort((a, b) => a.username.localeCompare(b.username));
  }, [usersQuery.data, search]);

  // Compute as soon as USERS load; runs are best-effort. If the runs query is still loading or has
  // errored, dashboardStats simply reports "never"/"—" for the last-run fields rather than leaving
  // the whole stat strip shimmering forever (which a runs-query failure used to do).
  const stats = usersQuery.data
    ? dashboardStats(usersQuery.data, runsQuery.data ?? [])
    : null;

  return (
    <div>
      <PageHeader
        icon={Gauge}
        title="Dashboard"
        subtitle={scheduleSubtitle}
        actions={
          <>
            <PrivacyBadge status={privacyQuery.data} />
            <Button
              variant="outline"
              size="sm"
              onClick={() => privacyCheck.mutate({ probe: false })}
              loading={privacyCheck.isPending}
            >
              <ShieldCheck aria-hidden="true" />
              Check now
            </Button>
          </>
        }
      />

      {privacyCheck.isError && (
        <p role="alert" className="mb-4 text-sm text-destructive">
          {apiErrorMessage(
            privacyCheck.error,
            "The Privacy Check could not run. Try again from Settings.",
          )}
        </p>
      )}

      {/* A refused run (the write gate says why) must be readable, not just a card that stops
          spinning. Same for a rejected enable/disable — the Switch snaps back to the server's
          answer, which without this reads as the click never landing. */}
      {startRun.isError && (
        <MutationAlert
          className="mb-4"
          error={startRun.error}
          fallback="Couldn’t start that run. Check the server log and try again."
        />
      )}

      {patchUser.isError && (
        <MutationAlert
          className="mb-4"
          error={patchUser.error}
          fallback="Couldn’t change that person’s row. Try again."
          onRetry={() => {
            const last = patchUser.variables;
            if (last) patchUser.mutate(last);
          }}
        />
      )}

      {stats ? (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile
            icon={UsersIcon}
            label="Enabled"
            value={stats.enabled}
            hint={`of ${stats.total} users`}
          />
          <StatTile icon={Clock} label="Last run" value={stats.lastRunAgo} />
          <StatTile
            icon={AlertTriangle}
            label="Errors"
            value={stats.errors ?? "—"}
            tone={stats.errors ? "destructive" : "default"}
            hint="last run"
          />
          <StatTile
            icon={Target}
            label="Picks watched"
            value={stats.hitRate}
            hint="across all users"
          />
        </div>
      ) : (
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-[4.75rem] w-full rounded-lg" />
          ))}
        </div>
      )}

      <div className="relative mb-4 max-w-xs">
        <Search
          className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
          aria-hidden="true"
        />
        <Input
          type="search"
          placeholder="Search users…"
          aria-label="Search users"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="pl-9"
        />
      </div>

      <QueryBoundary
        query={usersQuery}
        skeleton={<DashboardSkeleton />}
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Shortlist hasn't imported any Plex users. Finish the setup wizard, or check the Plex connection under Settings."
          />
        }
      >
        {() =>
          filteredUsers.length === 0 ? (
            <EmptyState
              title="No users match your search"
              hint="Try a different name, or clear the search box."
            />
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
              {filteredUsers.map((user) => (
                <UserCard
                  key={user.id}
                  user={user}
                  activeStage={
                    activeStages[user.slug] ??
                    activeStages[user.username] ??
                    null
                  }
                  runPending={pendingRunUserIds.has(user.id)}
                  onRunNow={handleRunNow}
                  onToggleEnabled={(target, enabled) =>
                    patchUser.mutate({ id: target.id, patch: { enabled } })
                  }
                />
              ))}
            </div>
          )
        }
      </QueryBoundary>
    </div>
  );
}
