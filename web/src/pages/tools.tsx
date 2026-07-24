import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  RefreshCw,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import { useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ProgressBar } from "@/components/ui/progress-bar";
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queries";
import { useSSE } from "@/lib/sse";
import type { SyncFinishedEvent, SyncProgressEvent } from "@/lib/types";

/**
 * Tools — on-demand maintenance the owner runs by hand, distinct from the nightly schedule. Each
 * action here is a deliberate "reconcile now" for when something has drifted; none of them writes
 * to Plex. Every card handles its own pending / error / success states inline.
 */
export function ToolsPage() {
  const queryClient = useQueryClient();
  // One EventSource for the whole page (rules/frontend.md); the two sync cards read the slice of
  // `sync.*` events that carries their own `kind`. `null` = idle, so no bar shows until a run starts.
  const [watchedProgress, setWatchedProgress] =
    useState<SyncProgressEvent | null>(null);
  const [usersProgress, setUsersProgress] = useState<SyncProgressEvent | null>(
    null,
  );
  // The watched sync's POST returns the moment it's queued (202 "started"), so its OUTCOME only
  // arrives on the bus. The users sync's POST awaits the whole thing, so its mutation result is
  // authoritative — the bus just drives its live bar.
  const [watchedResult, setWatchedResult] = useState<SyncFinishedEvent | null>(
    null,
  );

  useSSE({
    onSyncProgress: (event) => {
      if (event.kind === "watched") {
        setWatchedProgress(event);
        setWatchedResult(null); // a fresh run supersedes the last result line
      } else {
        setUsersProgress(event);
      }
    },
    onSyncFinished: (event) => {
      // Clear the bar once the sync ends; the card's own success/error line takes over from here.
      if (event.kind === "watched") {
        setWatchedProgress(null);
        setWatchedResult(event);
        // The watched sync refreshes each user's picks-watched — repaint the users list once done.
        queryClient.invalidateQueries({ queryKey: queryKeys.users });
      } else {
        setUsersProgress(null);
      }
    },
  });

  return (
    <div>
      <PageHeader
        icon={Wrench}
        title="Tools"
        subtitle="On-demand maintenance. Run these when something has drifted — a new user, or watched state that's out of sync — rather than waiting for the nightly run."
      />
      <div className="grid gap-4">
        <SyncHistoryCard progress={watchedProgress} result={watchedResult} />
        <SyncUsersCard progress={usersProgress} />
      </div>
    </div>
  );
}

/** Re-read every user's complete watched set now, rather than waiting for the nightly sync. */
function SyncHistoryCard({
  progress,
  result,
}: {
  progress: SyncProgressEvent | null;
  result: SyncFinishedEvent | null;
}) {
  const sync = useMutation({ mutationFn: api.syncWatched });
  // This POST returns 202 the moment the sync is QUEUED — the real outcome arrives on the bus as
  // `result`. So the bar is live while events flow, then the bus result (not the POST) is the truth.
  const running = progress !== null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <RefreshCw
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync watch history now
        </CardTitle>
        <CardDescription>
          Re-read every user's complete watched set from Plex right now —
          including anything they've marked as watched. This runs automatically
          each day; use it when you want the effectiveness report refreshed
          straight away.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={sync.isPending || running}
          >
            <RefreshCw aria-hidden="true" />
            Sync history
          </Button>
        </div>
        {running && (
          <div className="flex flex-col gap-1.5">
            <ProgressBar
              done={progress.done}
              total={progress.total}
              label="Syncing watch history"
            />
            <p role="status" className="text-xs text-muted-foreground">
              {progress.total
                ? `Syncing ${progress.done ?? 0} of ${progress.total} ${progress.total === 1 ? "user" : "users"}…`
                : "Syncing…"}
            </p>
          </div>
        )}
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't start the sync. Check the Plex connection and try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {!running && result?.ok === false && (
          <p role="alert" className="text-sm text-destructive">
            The sync couldn't finish
            {result.error ? ` (${result.error})` : ""}. Check the Plex
            connection and try again.
          </p>
        )}
        {!running && result?.ok && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            Synced {result.count ?? 0} {result.count === 1 ? "user" : "users"} —
            watch history is up to date and the effectiveness report reflects it
            now.
          </p>
        )}
        {/* No bus result yet (SSE not connected) but the POST was accepted — say it's running. */}
        {!running && !result && sync.isSuccess && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            Sync started — it runs in the background across every user. The
            effectiveness report updates on its own once it finishes.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** Re-pull the shared + Home users (and the owner) from plex.tv into the users table. */
function SyncUsersCard({ progress }: { progress: SyncProgressEvent | null }) {
  const queryClient = useQueryClient();
  const sync = useMutation({
    mutationFn: api.syncUsers,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
  const result = sync.data;
  // This POST awaits the whole sync, so `sync.data` is the authoritative result. The bus events just
  // drive the live bar while it's in flight: an indeterminate "fetch" phase, then a "save" count.
  const running = sync.isPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UsersIcon
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync users
        </CardTitle>
        <CardDescription>
          Re-pull everyone you share with — and yourself — from plex.tv and
          Tautulli (if connected). Refreshes usernames, display names/friendly
          names, and share status. Use it after inviting someone new so they
          show up in the user list without waiting for the next run.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={running}
          >
            <UsersIcon aria-hidden="true" />
            Sync users
          </Button>
        </div>
        {running && (
          <div className="flex flex-col gap-1.5">
            <ProgressBar
              done={progress?.phase === "save" ? progress.done : undefined}
              total={progress?.phase === "save" ? progress.total : undefined}
              label="Syncing users"
            />
            <p role="status" className="text-xs text-muted-foreground">
              {progress?.phase === "save" && progress.total
                ? `Saving ${progress.done ?? 0} of ${progress.total} ${progress.total === 1 ? "user" : "users"}…`
                : "Contacting plex.tv…"}
            </p>
          </div>
        )}
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't reach plex.tv to refresh the user list. Try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {result && !running && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            {result.added > 0 || result.updated > 0
              ? `Synced ${result.total} ${result.total === 1 ? "user" : "users"} — ${result.added} added, ${result.updated} updated.`
              : `All ${result.total} ${result.total === 1 ? "user is" : "users are"} already up to date.`}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
