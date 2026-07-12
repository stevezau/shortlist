import { ArrowLeft, Loader2, RefreshCw } from "lucide-react";
import { useId, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { formatHitRate, timeAgo } from "@/lib/format";
import {
  usePatchUser,
  useRun,
  useRuns,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import type { User } from "@/lib/types";

const DEFAULT_ROW_SIZE = 15;
const ROW_SIZES = [10, 15, 20];

function renderRowName(template: string): string {
  return template.replace("{top_seed}", "Fargo");
}

function CurrentPicks({ user }: { user: User }) {
  const runsQuery = useRuns();
  const latestFinished = runsQuery.data?.find(
    (run) => run.finished_at !== null,
  );
  const runQuery = useRun(
    latestFinished?.id ?? 0,
    latestFinished !== undefined,
  );

  if (runsQuery.isPending || (latestFinished && runQuery.isPending)) {
    return <Skeleton className="h-40 w-full" />;
  }
  if (runsQuery.isError || runQuery.isError) {
    return (
      <p className="text-sm text-muted-foreground">
        Couldn't load the latest run, so current picks aren't shown. See the
        Runs page for details.
      </p>
    );
  }

  const result = runQuery.data?.users.find((entry) => entry.slug === user.slug);
  if (!result || result.picks.length === 0) {
    return (
      <EmptyState
        title="No picks yet"
        hint="This user hasn't had a run yet, or the last run produced nothing. Use Regenerate to build their row now."
      />
    );
  }

  return (
    <ol className="space-y-2">
      {result.picks
        .slice()
        .sort((a, b) => a.rank - b.rank)
        .map((pick) => (
          <li
            key={pick.rank}
            className="flex items-baseline gap-3 rounded-md border p-3"
          >
            <span className="text-sm font-semibold text-primary">
              #{pick.rank}
            </span>
            <div>
              <p className="font-medium">{pick.title}</p>
              <p className="text-sm text-muted-foreground">{pick.reason}</p>
            </div>
          </li>
        ))}
    </ol>
  );
}

function UserDetailBody({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const startRun = useStartRun();

  // GET /api/users doesn't return prefs yet, so overrides start from defaults
  // rather than the saved values. TODO: initialize from the API once prefs are
  // exposed on the user read model.
  const [rowNameTpl, setRowNameTpl] = useState("");
  const [rowSize, setRowSize] = useState(DEFAULT_ROW_SIZE);
  const [paused, setPaused] = useState(false);

  const rowNameId = useId();
  const pausedId = useId();

  const saveOverrides = () => {
    patchUser.mutate({
      id: user.id,
      patch: {
        prefs: {
          ...(rowNameTpl ? { row_name_tpl: rowNameTpl } : {}),
          row_size: rowSize,
        },
      },
    });
  };

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <Link
          to="/users"
          className="inline-flex items-center gap-1 rounded-sm text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          All users
        </Link>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {user.username}
          </h1>
          {user.user_type === "managed" && (
            <Badge variant="secondary">managed</Badge>
          )}
          {user.user_type === "owner" && (
            <Badge variant="outline">owner — never restricted</Badge>
          )}
          {user.cold_start && <Badge variant="warning">cold start</Badge>}
        </div>
        <p className="text-sm text-muted-foreground">
          {user.history_depth} history items · last run{" "}
          {timeAgo(user.last_run_at)} · hit rate {formatHitRate(user.hit_rate)}
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Current picks</CardTitle>
          <CardDescription>
            What's in this user's row right now, and why each title made the
            cut.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <CurrentPicks user={user} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Overrides</CardTitle>
          <CardDescription>
            Settings here apply to {user.username} only and win over the global
            defaults.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor={rowNameId}>Row name</Label>
            <Input
              id={rowNameId}
              value={rowNameTpl}
              onChange={(event) => setRowNameTpl(event.target.value)}
              placeholder="✨ Picked for You"
            />
            {rowNameTpl.includes("{top_seed}") && (
              <p className="text-sm text-muted-foreground">
                Preview:{" "}
                <span className="text-foreground">
                  {renderRowName(rowNameTpl)}
                </span>
              </p>
            )}
          </div>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Row size</legend>
            <div className="flex gap-2">
              {ROW_SIZES.map((size) => (
                <Button
                  key={size}
                  type="button"
                  size="sm"
                  variant={rowSize === size ? "default" : "outline"}
                  aria-pressed={rowSize === size}
                  onClick={() => setRowSize(size)}
                >
                  {size}
                </Button>
              ))}
            </div>
          </fieldset>
          <div className="flex items-center gap-3">
            <Switch
              id={pausedId}
              checked={paused}
              onCheckedChange={(next) => {
                setPaused(next);
                patchUser.mutate({
                  id: user.id,
                  patch: { prefs: { paused: next } },
                });
              }}
            />
            <Label htmlFor={pausedId}>
              Paused — keep the row but stop refreshing it
            </Label>
          </div>
          <div className="flex gap-2">
            <Button onClick={saveOverrides} disabled={patchUser.isPending}>
              {patchUser.isPending && (
                <Loader2 className="animate-spin" aria-hidden="true" />
              )}
              Save overrides
            </Button>
            <Button
              variant="secondary"
              onClick={() => startRun.mutate({ user_ids: [user.id] })}
              disabled={startRun.isPending}
            >
              <RefreshCw aria-hidden="true" />
              Regenerate row now
            </Button>
          </div>
          {startRun.isSuccess && (
            <p className="text-sm text-muted-foreground">
              Run started — watch it live on the Dashboard.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function UserDetailPage() {
  const { id } = useParams();
  const userId = Number(id);
  const usersQuery = useUsers();

  return (
    <QueryBoundary
      query={usersQuery}
      skeleton={<Skeleton className="h-64 w-full" />}
      isEmpty={(users) => !users.some((user) => user.id === userId)}
      empty={
        <EmptyState
          title="User not found"
          hint="This user may have been removed from the Plex server. Head back to the Users list."
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/users">Back to Users</Link>
            </Button>
          }
        />
      }
    >
      {(users) => {
        const user = users.find((entry) => entry.id === userId);
        return user ? <UserDetailBody user={user} /> : null;
      }}
    </QueryBoundary>
  );
}
