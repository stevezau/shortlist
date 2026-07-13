import { CheckCircle2, Clock, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { UserAvatar } from "@/components/user-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ApiError } from "@/lib/api";
import {
  formatDate,
  formatHitRate,
  runStatusVariant,
  timeAgo,
} from "@/lib/format";
import {
  usePatchUser,
  useSetUserRowOverride,
  useStartRun,
  useUserHistory,
  useUserRows,
  useUserRuns,
  useUsers,
} from "@/lib/queries";
import type { User, UserRow } from "@/lib/types";

const SIZE_OPTIONS = [
  { value: "default", label: "Default" },
  { value: "10", label: "10" },
  { value: "15", label: "15" },
  { value: "20", label: "20" },
];

/** One of a person's rows: its live picks, and a per-person customization drawer. */
function RowCard({ userId, row }: { userId: number; row: UserRow }) {
  const save = useSetUserRowOverride(userId);
  const [open, setOpen] = useState(false);
  const [muted, setMuted] = useState(row.muted);
  const [size, setSize] = useState<string>(
    row.override.row_size ? String(row.override.row_size) : "default",
  );
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: row.override.prompt_tone,
    guidance: row.override.prompt_guidance,
    template: row.override.prompt_template,
  });
  const [saved, setSaved] = useState(false);

  // The mute switch sends ONLY {muted} so it can't persist unsaved drawer edits; the drawer's
  // Save button sends size + curation (the server only writes the fields it receives).
  const setMutedOnly = (nextMuted: boolean) => {
    setMuted(nextMuted);
    save.mutate({
      collectionId: row.collection_id,
      patch: { muted: nextMuted },
    });
  };

  const saveCustomization = () => {
    setSaved(false);
    save.mutate(
      {
        collectionId: row.collection_id,
        patch: {
          row_size: size === "default" ? null : Number(size),
          prompt_tone: curation.tone,
          prompt_guidance: curation.guidance,
          prompt_template: curation.template,
        },
      },
      { onSuccess: () => setSaved(true) },
    );
  };

  return (
    <Card className={muted ? "opacity-60" : ""}>
      <CardContent className="space-y-4 pt-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{row.name}</span>
              {row.is_default && <Badge variant="outline">default</Badge>}
              {muted && <Badge variant="secondary">muted</Badge>}
            </div>
            <p className="text-sm text-muted-foreground">
              {row.override.row_size ?? row.size} titles ·{" "}
              {row.media === "both" ? "movies & shows" : `${row.media}s`}
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            {muted ? "Off for them" : "On"}
            <Switch
              checked={!muted}
              onCheckedChange={(on) => setMutedOnly(!on)}
              aria-label={`Show ${row.name} for this person`}
            />
          </label>
        </div>

        {!muted &&
          (row.picks.length > 0 ? (
            <ol className="space-y-1.5">
              {row.picks.map((pick) => (
                <li
                  key={pick.rank}
                  className="flex items-baseline gap-3 text-sm"
                >
                  <span className="w-5 shrink-0 font-semibold text-primary">
                    #{pick.rank}
                  </span>
                  <span>
                    <span className="font-medium">{pick.title}</span>
                    <span className="text-muted-foreground">
                      {" "}
                      — {pick.reason}
                      {pick.seed_title
                        ? ` · inspired by ${pick.seed_title}`
                        : ""}
                    </span>
                  </span>
                </li>
              ))}
            </ol>
          ) : (
            <p className="text-sm text-muted-foreground">
              No picks in this row yet — regenerate below or wait for the next
              run.
            </p>
          ))}

        <div className="border-t pt-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? "Hide customization" : "Customize for this person"}
          </Button>

          {open && (
            <div className="mt-3 space-y-4">
              <Segmented
                legend="Row size"
                value={size}
                options={SIZE_OPTIONS}
                onChange={setSize}
              />
              <div className="space-y-2">
                <p className="text-sm font-medium">Curation style</p>
                <p className="text-sm text-muted-foreground">
                  Leave a field blank to use this row&rsquo;s own style.
                </p>
                <CurationStyleFields value={curation} onChange={setCuration} />
              </div>
              <div className="flex items-center gap-3">
                <Button
                  size="sm"
                  onClick={saveCustomization}
                  loading={save.isPending}
                >
                  Save
                </Button>
                {saved && !save.isPending && (
                  <span className="flex items-center gap-1.5 text-sm text-success">
                    <CheckCircle2 className="h-4 w-4" aria-hidden="true" />
                    Saved
                  </span>
                )}
                {save.isError && (
                  <span role="alert" className="text-sm text-destructive">
                    {save.error instanceof ApiError
                      ? save.error.message
                      : "Save failed."}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function RowsSection({ user }: { user: User }) {
  const query = useUserRows(user.id);
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-40 w-full" />}
      isEmpty={(rows) => rows.length === 0}
      empty={
        <EmptyState
          title="No rows reach this person"
          hint="They aren’t in the audience of any row yet. Add a row, or set its audience to include them, on the Rows page."
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/rows">Go to Rows</Link>
            </Button>
          }
        />
      }
    >
      {(rows) => (
        <div className="space-y-3">
          {rows.map((row) => (
            <RowCard key={row.collection_id} userId={user.id} row={row} />
          ))}
        </div>
      )}
    </QueryBoundary>
  );
}

function WatchHistory({ userId }: { userId: number }) {
  const query = useUserHistory(userId);
  if (query.isPending) return <Skeleton className="h-40 w-full" />;
  if (query.isError) {
    return (
      <div className="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-4">
        <p className="text-sm text-destructive">
          Couldn’t load watch history — check the Plex/Tautulli connection.
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
        >
          Try again
        </Button>
      </div>
    );
  }
  if (query.data.length === 0) {
    return (
      <EmptyState
        title="No watch history"
        hint="Rowarr sees nothing this person has watched yet — recommendations start once they do."
      />
    );
  }
  return (
    <ul className="divide-y">
      {query.data.map((item, i) => (
        <li key={i} className="flex items-baseline justify-between gap-3 py-2">
          <span className="text-sm">
            <span className="font-medium">{item.title}</span>
            {item.year ? (
              <span className="text-muted-foreground"> ({item.year})</span>
            ) : null}
          </span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {item.media_type === "show" ? "Show" : "Movie"} ·{" "}
            {timeAgo(item.watched_at)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function RecentRuns({ userId }: { userId: number }) {
  const query = useUserRuns(userId);
  if (query.isPending) return <Skeleton className="h-32 w-full" />;
  if (query.isError) {
    return (
      <div className="space-y-3 rounded-md border border-destructive/30 bg-destructive/5 p-4">
        <p className="text-sm text-destructive">
          Couldn’t load this person’s runs.
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void query.refetch()}
        >
          Try again
        </Button>
      </div>
    );
  }
  if (query.data.length === 0) {
    return (
      <EmptyState
        title="No runs yet"
        hint="Once a run processes this person, each one shows up here with what it changed and why."
      />
    );
  }
  return (
    <ul className="space-y-2">
      {query.data.map((run) => {
        const added = run.diff.added?.length ?? 0;
        const removed = run.diff.removed?.length ?? 0;
        return (
          <li key={run.run_id} className="rounded-md border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Link
                  to={`/runs/${run.run_id}`}
                  className="font-medium hover:text-primary hover:underline"
                >
                  Run #{run.run_id}
                </Link>
                <Badge variant={runStatusVariant(run.status)}>
                  {run.status}
                </Badge>
                {run.dry_run && <Badge variant="outline">dry-run</Badge>}
              </div>
              <span className="text-xs text-muted-foreground">
                {run.finished_at
                  ? formatDate(run.finished_at)
                  : formatDate(run.started_at)}
              </span>
            </div>
            {run.error ? (
              <p className="mt-1 font-mono text-xs text-destructive">
                {run.error}
              </p>
            ) : (
              <p className="mt-1 text-xs text-muted-foreground">
                +{added} added · −{removed} removed · {run.picks.length} picks
              </p>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return <h2 className="text-lg font-semibold">{children}</h2>;
}

function UserDetailBody({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const startRun = useStartRun();
  const paused = user.prefs?.paused ?? false;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
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
              {paused && <Badge variant="secondary">paused</Badge>}
            </div>
            <p className="text-sm text-muted-foreground">
              {user.history_depth} titles watched · last run{" "}
              {timeAgo(user.last_run_at)} · {formatHitRate(user.hit_rate)} of
              picks watched
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            {paused ? "Paused" : "Active"}
            <Switch
              checked={!paused}
              onCheckedChange={(active) =>
                patchUser.mutate({
                  id: user.id,
                  patch: { prefs: { paused: !active } },
                })
              }
              aria-label={`Pause or resume ${user.username}`}
            />
          </label>
          <Button
            variant="secondary"
            onClick={() => startRun.mutate({ user_ids: [user.id] })}
            loading={startRun.isPending}
          >
            {!startRun.isPending && <RefreshCw aria-hidden="true" />}
            Regenerate now
          </Button>
        </div>
      </header>

      {startRun.isSuccess && (
        <p className="text-sm text-muted-foreground">
          Run started — watch it live on the Dashboard.
        </p>
      )}

      <section className="space-y-3">
        <SectionHeading>Their rows</SectionHeading>
        <p className="text-sm text-muted-foreground">
          Every row {user.username} gets, with its current picks and why each
          was chosen. Customize any of them for this person only.
        </p>
        <RowsSection user={user} />
      </section>

      <section className="space-y-3">
        <SectionHeading>Watch history</SectionHeading>
        <Card>
          <CardContent className="pt-6">
            <WatchHistory userId={user.id} />
          </CardContent>
        </Card>
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <SectionHeading>Recent runs</SectionHeading>
          <Button asChild variant="ghost" size="sm">
            <Link to="/runs">
              <Clock aria-hidden="true" />
              All runs
            </Link>
          </Button>
        </div>
        <RecentRuns userId={user.id} />
      </section>
    </div>
  );
}

export function UserDetailPage() {
  const { id } = useParams();
  const userId = Number(id);
  const usersQuery = useUsers();

  return (
    <div className="space-y-6">
      <BackLink to="/users" label="All users" />
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
    </div>
  );
}
