import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  DatabaseZap,
  RefreshCw,
  Users as UsersIcon,
  Wrench,
} from "lucide-react";
import { useState } from "react";
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
  const [setupOpen, setSetupOpen] = useState(false);
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
          Plex fixed a long-standing bug: items you mark-as-watched now create
          history entries (they didn't before). But anything marked before the
          fix is still invisible to Shortlist's usual sync. This is a{" "}
          <strong>one-time manual sync</strong> that reads Plex's database
          directly — the only source that sees marks — and fills those gaps.
          After running it once, the regular nightly sync keeps everyone
          current.
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
          <>
            <p className="text-sm text-muted-foreground">
              No Plex database is mounted yet.{" "}
              <button
                onClick={() => setSetupOpen(!setupOpen)}
                className="inline-flex items-center gap-1 font-medium underline underline-offset-4"
              >
                How to set it up
                {setupOpen ? (
                  <ChevronUp className="size-3" />
                ) : (
                  <ChevronDown className="size-3" />
                )}
              </button>
            </p>
            {setupOpen && (
              <div className="space-y-3 rounded-md border bg-muted/40 p-4 text-sm">
                <div>
                  <p className="font-medium">Docker setup (recommended)</p>
                  <ol className="ml-4 mt-2 list-decimal space-y-1.5 text-muted-foreground">
                    <li>
                      Find your Plex database file. It's usually at:
                      <ul className="ml-4 mt-1 list-disc">
                        <li>
                          Linux:{" "}
                          <code className="rounded bg-background px-1 py-0.5">
                            /var/lib/plexmediaserver/Library/Application
                            Support/Plex Media Server/Plug-in
                            Support/Databases/com.plexapp.plugins.library.db
                          </code>
                        </li>
                        <li>
                          macOS:{" "}
                          <code className="rounded bg-background px-1 py-0.5">
                            ~/Library/Application Support/Plex Media
                            Server/Plug-in
                            Support/Databases/com.plexapp.plugins.library.db
                          </code>
                        </li>
                      </ul>
                    </li>
                    <li>
                      Mount it <strong>read-only</strong> into your Shortlist
                      container at <code>/plexdb</code>:
                      <pre className="mt-1 rounded bg-background p-2 text-xs">
                        {`-v /path/to/com.plexapp.plugins.library.db:/plexdb:ro`}
                      </pre>
                    </li>
                    <li>
                      Set the path in{" "}
                      <Link
                        className="font-medium underline underline-offset-4"
                        to="/settings#connections"
                      >
                        Settings → Connections
                      </Link>{" "}
                      to <code>/plexdb</code>
                    </li>
                    <li>Come back here and run Reconcile</li>
                  </ol>
                </div>
                <p className="text-xs italic">
                  The mount is read-only — Shortlist never writes to Plex's
                  database.
                </p>
              </div>
            )}
          </>
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
          <strong>Note:</strong> Plex fixed this in recent versions — new
          mark-as-watched actions now appear in history automatically. This tool
          is for backfilling old marks only. You shouldn't need to run it again
          after the first time.
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
