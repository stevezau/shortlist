import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Clock,
  Gauge,
  Search,
  Target,
  Users as UsersIcon,
} from "lucide-react";
import { useMemo, useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { StatTile } from "@/components/stat-tile";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { UserCard } from "@/components/user-card";
import { dashboardStats } from "@/lib/dashboard-stats";
import {
  queryKeys,
  usePatchUser,
  useRuns,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { User } from "@/lib/types";

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
  });

  // Schedules are per-row now (each row's editor), so there's no single cadence to name here.
  const scheduleSubtitle =
    "Private “Picked for You” rows — each refreshes on its own schedule.";

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
      <PageHeader icon={Gauge} title="Dashboard" subtitle={scheduleSubtitle} />

      {/* A run that fails to start must say why, not just leave a card that stops spinning. Same
          for a rejected enable/disable — the Switch snaps back to the server's answer, which
          without this reads as the click never landing. */}
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
