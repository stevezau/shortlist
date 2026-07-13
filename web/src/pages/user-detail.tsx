import { CheckCircle2, RefreshCw } from "lucide-react";
import { useId, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
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
import {
  formatHitRate,
  renderRowName,
  settingString,
  timeAgo,
} from "@/lib/format";
import {
  usePatchUser,
  useRun,
  useRuns,
  useSettings,
  useStartRun,
  useUsers,
} from "@/lib/queries";
import type { User } from "@/lib/types";

const DEFAULT_ROW_SIZE = 15;
const ROW_SIZES = [10, 15, 20];

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

  // Rehydrate the override form from the saved prefs so a reload shows what
  // was actually saved, not blank defaults.
  const [rowNameTpl, setRowNameTpl] = useState(user.prefs?.row_name_tpl ?? "");
  const [rowSize, setRowSize] = useState(
    user.prefs?.row_size ?? DEFAULT_ROW_SIZE,
  );
  const [paused, setPaused] = useState(user.prefs?.paused ?? false);
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: user.prefs?.prompt_tone ?? "",
    guidance: user.prefs?.prompt_guidance ?? "",
    template: user.prefs?.prompt_template ?? "",
  });
  // The pause switch and the "Save overrides" button share one mutation but are separate actions;
  // track the form save alone so its "Saved" tick doesn't flash when the switch is toggled.
  const [savedOverrides, setSavedOverrides] = useState(false);

  const settingsQuery = useSettings();
  const globalDefaults: CurationStyleValue = {
    tone: settingsQuery.data
      ? settingString(settingsQuery.data, "curator.prompt_tone", "balanced")
      : "balanced",
    guidance: settingsQuery.data
      ? settingString(settingsQuery.data, "curator.prompt_guidance")
      : "",
    template: settingsQuery.data
      ? settingString(settingsQuery.data, "curator.prompt_template")
      : "",
  };

  const rowNameId = useId();
  const pausedId = useId();

  const saveOverrides = () => {
    setSavedOverrides(false);
    patchUser.mutate(
      {
        id: user.id,
        patch: {
          prefs: {
            ...(rowNameTpl ? { row_name_tpl: rowNameTpl } : {}),
            row_size: rowSize,
            // Empty string = inherit the global default, which the backend honours.
            prompt_tone: curation.tone,
            prompt_guidance: curation.guidance,
            prompt_template: curation.template,
          },
        },
      },
      { onSuccess: () => setSavedOverrides(true) },
    );
  };

  return (
    <div className="space-y-6">
      <header className="space-y-3">
        <BackLink to="/users" label="All users" />
        <div className="flex items-center gap-4">
          <UserAvatar name={user.username} size="lg" />
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                {user.username}
              </h1>
              {user.user_type === "managed" && (
                <Badge variant="secondary">managed</Badge>
              )}
              {user.user_type === "owner" && (
                <Badge
                  variant="outline"
                  title="Plex cannot hide rows from the server owner"
                >
                  owner
                </Badge>
              )}
              {user.cold_start && (
                <Badge
                  variant="warning"
                  title="Not enough watch history yet — starting from popular titles"
                >
                  cold start
                </Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              {user.history_depth} titles watched · last run{" "}
              {timeAgo(user.last_run_at)} · {formatHitRate(user.hit_rate)} of
              picks watched
            </p>
          </div>
        </div>
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
          <div className="space-y-3 border-t pt-4">
            <div>
              <p className="text-sm font-medium">Curation style</p>
              <p className="text-sm text-muted-foreground">
                Override how the AI writes {user.username}&rsquo;s row. Leave a
                field on its default to inherit the global setting.
              </p>
            </div>
            <CurationStyleFields
              value={curation}
              onChange={setCuration}
              perPerson
              globalDefaults={globalDefaults}
            />
          </div>
          <div className="flex items-center gap-3 border-t pt-4">
            <Switch
              id={pausedId}
              checked={paused}
              onCheckedChange={(next) => {
                setPaused(next);
                setSavedOverrides(false);
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
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={saveOverrides} loading={patchUser.isPending}>
              Save overrides
            </Button>
            <Button
              variant="secondary"
              onClick={() => startRun.mutate({ user_ids: [user.id] })}
              loading={startRun.isPending}
            >
              {!startRun.isPending && <RefreshCw aria-hidden="true" />}
              Regenerate row now
            </Button>
            {savedOverrides && !patchUser.isPending && (
              <p
                role="status"
                className="flex items-center gap-1.5 text-sm text-success"
              >
                <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                Saved
              </p>
            )}
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
