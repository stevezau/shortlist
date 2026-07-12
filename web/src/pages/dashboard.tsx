import { useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, ShieldCheck, ShieldQuestion } from "lucide-react";
import { useMemo, useState } from "react";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { UserCard } from "@/components/user-card";
import {
  formatHitRate,
  settingString,
  timeAgo,
  timeFromCron,
} from "@/lib/format";
import {
  queryKeys,
  usePatchUser,
  usePrivacyStatus,
  useRuns,
  useSettings,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { PrivacyStatus, Run, User } from "@/lib/types";

function PrivacyBadge({ status }: { status: PrivacyStatus | undefined }) {
  if (!status || status.passed === null) {
    return (
      <Badge variant="warning">
        <ShieldQuestion className="h-3 w-3" aria-hidden="true" />
        Privacy: not checked yet
      </Badge>
    );
  }
  if (status.passed) {
    return (
      <Badge variant="success">
        <ShieldCheck className="h-3 w-3" aria-hidden="true" />
        Privacy: verified{" "}
        {status.last_check
          ? new Date(status.last_check).toLocaleDateString()
          : ""}
      </Badge>
    );
  }
  return (
    <Badge variant="destructive">
      <ShieldAlert className="h-3 w-3" aria-hidden="true" />
      Privacy: check failed — rows may be visible to others
    </Badge>
  );
}

function summaryLine(users: User[], runs: Run[]): string {
  const enabled = users.filter((user) => user.enabled).length;
  const lastRun = runs[0];
  const lastRunText = lastRun
    ? `last run ${timeAgo(lastRun.started_at)}`
    : "no runs yet";
  const errorsText = lastRun ? `${lastRun.stats.users_error} errors` : "—";
  const rates = users
    .map((user) => user.hit_rate)
    .filter((rate): rate is number => rate !== null);
  const hitRate =
    rates.length > 0
      ? formatHitRate(rates.reduce((a, b) => a + b, 0) / rates.length)
      : "—";
  return `${enabled} users enabled · ${lastRunText} · ${errorsText} · hit rate ${hitRate}`;
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
  const scheduleTime = scheduleCron ? timeFromCron(scheduleCron).time : "";

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

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Dashboard</h1>
        <div className="flex flex-wrap items-center gap-3">
          <PrivacyBadge status={privacyQuery.data} />
          <span className="text-sm text-muted-foreground">
            Next run:{" "}
            {scheduleTime ? `tonight ${scheduleTime}` : "not scheduled yet"}
          </span>
        </div>
      </header>

      {usersQuery.data && runsQuery.data && (
        <p className="text-sm text-muted-foreground">
          {summaryLine(usersQuery.data, runsQuery.data)}
        </p>
      )}

      <div className="max-w-xs">
        <Input
          type="search"
          placeholder="Search users…"
          aria-label="Search users"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      <QueryBoundary
        query={usersQuery}
        skeleton={<DashboardSkeleton />}
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Rowarr hasn't imported any Plex users. Finish the setup wizard, or check the Plex connection under Settings."
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
