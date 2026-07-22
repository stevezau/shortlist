import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  DatabaseZap,
  RefreshCw,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import { Link } from "react-router-dom";

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
import { api } from "@/lib/api";
import { queryKeys } from "@/lib/queries";

/**
 * Tools — on-demand maintenance the owner runs by hand, distinct from the nightly schedule. Each
 * action here is a deliberate "reconcile now" for when something has drifted; none of them writes
 * to Plex. Every card handles its own pending / error / success states inline.
 */
export function ToolsPage() {
  return (
    <div>
      <PageHeader
        icon={Wrench}
        title="Tools"
        subtitle="On-demand maintenance. Run these when something has drifted — a new user, or watched state that's out of sync — rather than waiting for the nightly run."
      />
      <div className="grid gap-4">
        <ReconcileWatchedCard />
        <SyncHistoryCard />
        <SyncUsersCard />
      </div>
    </div>
  );
}

/** Fill watch history from Plex's database — the only source that sees a mark-as-watched. */
function ReconcileWatchedCard() {
  const queryClient = useQueryClient();
  const reconcile = useMutation({
    mutationFn: api.reconcileWatched,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });
  const result = reconcile.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <DatabaseZap
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Reconcile watched from Plex
        </CardTitle>
        <CardDescription>
          Plex's history only records things that were <em>played</em> — never a
          title you mark-as-watched. This reads watched state straight from
          Plex's own database (the one source that sees marks) and fills the
          gaps, so a film you ticked off elsewhere stops getting recommended
          back to you. It reads the database read-only and never changes
          anything in Plex.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <Button
            onClick={() => reconcile.mutate()}
            loading={reconcile.isPending}
          >
            <DatabaseZap aria-hidden="true" />
            Reconcile now
          </Button>
        </div>

        {reconcile.isError && (
          <MutationAlert
            error={reconcile.error}
            fallback="Couldn't read Plex's database. Check the mount and try again."
            onRetry={() => reconcile.mutate()}
          />
        )}

        {result && !result.configured && (
          <p className="text-sm text-muted-foreground">
            No Plex database is mounted, so there's nothing to read yet. Mount
            it read-only and point Shortlist at it under{" "}
            <Link
              className="font-medium underline underline-offset-4"
              to="/settings#connections"
            >
              Settings → Connections
            </Link>
            , then run this again.
          </p>
        )}

        {result?.configured && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            {result.added > 0
              ? `Added ${result.added} watched ${result.added === 1 ? "title" : "titles"} across ${result.users} ${result.users === 1 ? "user" : "users"} that the play history never saw.`
              : `Everyone's already in sync — the database held nothing the play history hadn't already recorded (checked ${result.users} ${result.users === 1 ? "user" : "users"}).`}
          </p>
        )}

        <p className="text-xs text-muted-foreground">
          One thing to know: the history API still can't see marks, so anything
          you mark-as-watched
          <em> after</em> a reconcile stays invisible until you run this again.
        </p>
      </CardContent>
    </Card>
  );
}

/** Pull the latest plays for everyone now, rather than waiting for the nightly watch-status sync. */
function SyncHistoryCard() {
  const sync = useMutation({ mutationFn: api.syncWatched });

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
          Pull the newest plays for every user from Plex (and Tautulli, if
          connected) right now. This runs automatically each day; use it when
          you want the effectiveness report refreshed straight away. It runs in
          the background.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={sync.isPending}
          >
            <RefreshCw aria-hidden="true" />
            Sync history
          </Button>
        </div>
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't start the sync. Check the Plex connection and try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {sync.isSuccess && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            Started — the report refreshes once it lands.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/** Re-pull the shared + Home users (and the owner) from plex.tv into the users table. */
function SyncUsersCard() {
  const queryClient = useQueryClient();
  const sync = useMutation({
    mutationFn: api.syncUsers,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <UsersIcon
            aria-hidden="true"
            className="size-5 text-muted-foreground"
          />
          Sync users from Plex
        </CardTitle>
        <CardDescription>
          Re-pull everyone you share with — and yourself — from plex.tv. Use it
          after inviting someone new so they show up in the user list without
          waiting for the next run. This is the same action as the button on the
          Users page.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div>
          <Button
            variant="outline"
            onClick={() => sync.mutate()}
            loading={sync.isPending}
          >
            <UsersIcon aria-hidden="true" />
            Sync users
          </Button>
        </div>
        {sync.isError && (
          <MutationAlert
            error={sync.error}
            fallback="Couldn't reach plex.tv to refresh the user list. Try again."
            onRetry={() => sync.mutate()}
          />
        )}
        {sync.isSuccess && (
          <p className="flex items-center gap-2 text-sm text-foreground">
            <CheckCircle2
              aria-hidden="true"
              className="size-4 text-emerald-600 dark:text-emerald-500"
            />
            User list refreshed.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
