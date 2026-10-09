import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, UserPlus } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { MutationAlert } from "@/components/mutation-alert";
import { TransferSteps } from "@/pages/watching-account";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";
import {
  queryKeys,
  usePatchUser,
  useSetAllUsersEnabled,
  useUsers,
} from "@/lib/queries";

/** Why this person has no row, said under their name so a switched-off switch is never a mystery. */
function offReason(user: User): string {
  if (user.user_type === "owner") return "This is you. Switch on to get a row of your own.";
  if (user.restricted) {
    return "Has a Plex restriction profile, which can limit how rows are hidden from them. Switch on to give them a row.";
  }
  return "Switched off, so they get no row. Switch on to give them one.";
}

/**
 * Step 4 — pick users. Syncs the user list from plex.tv on entry, then a
 * toggle per user with select-all, plus the owner caveat surfaced up front
 * (design doc §3 step 4). Takes no wizard props — the step is always
 * leaveable and edits users directly via the API.
 */
export function StepUsers() {
  const usersQuery = useUsers();
  const patchUser = usePatchUser();
  const queryClient = useQueryClient();

  const sync = useMutation({
    mutationFn: api.syncUsers,
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

  // Sync once when the step mounts so the table reflects plex.tv right now.
  const syncedRef = useRef(false);
  const syncMutate = sync.mutate;
  useEffect(() => {
    if (!syncedRef.current) {
      syncedRef.current = true;
      syncMutate();
    }
  }, [syncMutate]);

  // One bulk write flips everyone in a single transaction (and optimistically in the cache), so the
  // switches respond to the click instead of to 48 sequential per-user round-trips.
  const setAll = useSetAllUsersEnabled();

  // Users are created disabled, so a default click-through would build zero rows. Pre-select everyone
  // once, after the first sync — but only when NOBODY is enabled yet, so a returning owner who
  // deliberately turned people off is never re-enabled behind their back.
  const autoSelectedRef = useRef(false);
  const users = usersQuery.data;
  const setAllMutate = setAll.mutate;
  useEffect(() => {
    if (
      autoSelectedRef.current ||
      !sync.isSuccess ||
      !users ||
      users.length === 0
    )
      return;
    autoSelectedRef.current = true;
    if (!users.some((u) => u.enabled)) setAllMutate(true);
  }, [sync.isSuccess, users, setAllMutate]);

  const [showTransfer, setShowTransfer] = useState(false);

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-primary/40 bg-primary/10 p-4 text-sm">
        <p className="font-medium text-primary">Heads up, server owner</p>
        <p className="mt-1 text-muted-foreground">
          Plex cannot hide other people’s rows from you in a library’s <strong>Collections</strong> tab.
        </p>
        <details className="group mt-2">
          <summary className="cursor-pointer font-medium text-foreground">Why?</summary>
          <div className="mt-2 space-y-2 text-muted-foreground">
            <p>
              {/* Read from the switch's ACTUAL state: the pre-select only fires when nobody is
                  enabled yet, so a returning owner arrives switched OFF and must not be told
                  otherwise. */}
              {users?.find((user) => user.user_type === "owner")?.enabled ? (
                <>
                  You&rsquo;re in this list too, switched on like everyone else —
                  turn yourself off below if you&rsquo;d rather not have a row.
                </>
              ) : (
                <>
                  You&rsquo;re in this list too — switch yourself on below to get a
                  row of your own.
                </>
              )}{" "}
              Your Home screen shows your own rows. The same goes for the <strong>Recommended</strong>{" "}
              shelf: Plex cannot hide other rows from you, because the owner has no sharing
              restrictions. Managed accounts with parental profiles also have limits on hiding rows.
            </p>
            {/* The remedy happens HERE, while the owner is deciding whether to watch on this
                account (issue #85: told twice, found 22 rows on their shelf days later). Inline
                rather than a link: until setup completes every route redirects back to /setup.
                `TransferSteps` is the Watching account page's own flow, imported so it cannot drift. */}
            <p>
              <strong className="text-foreground">Do you watch on this admin account?</strong> If so,
              the usual fix is to watch on a separate Plex Home account and keep this one for running
              the server. Shortlist copies your watch history across, so its picks are right from the
              first run.
            </p>
            {!showTransfer ? (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <Button size="sm" variant="outline" onClick={() => setShowTransfer(true)}>
                  <UserPlus className="h-4 w-4" aria-hidden="true" />
                  Set that up now
                </Button>
                <span className="text-xs">
                  Or skip it &mdash; you can do this any time from <strong>Users</strong>, and
                  Shortlist will remind you once.
                </span>
              </div>
            ) : (
              <div>
                <TransferSteps numbered={false} />
                <Button variant="ghost" size="sm" className="mt-2" onClick={() => setShowTransfer(false)}>
                  Not now
                </Button>
              </div>
            )}
          </div>
        </details>
      </div>

      {/* Both writes here decide who gets a row at all, and the Switch mirrors the server — so a
          rejected one has to say so rather than just springing back. */}
      {(patchUser.isError || setAll.isError) && (
        <MutationAlert
          error={patchUser.error ?? setAll.error}
          fallback="Couldn’t change who gets a row. Try again."
          onRetry={() => {
            const one = patchUser.variables;
            const all = setAll.variables;
            if (patchUser.isError && one) patchUser.mutate(one);
            if (setAll.isError && all !== undefined) setAll.mutate(all);
          }}
        />
      )}

      <QueryBoundary
        query={usersQuery}
        skeleton={<Skeleton className="h-64 w-full" />}
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title={
              sync.isPending ? "Syncing users from plex.tv…" : "No users found"
            }
            hint={
              sync.isPending
                ? "One moment — fetching your shared and managed users."
                : "Your server has no shared or managed users yet. Invite someone on plex.tv, then sync again."
            }
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => sync.mutate()}
                disabled={sync.isPending}
              >
                {sync.isPending && (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                )}
                Sync again
              </Button>
            }
          />
        }
      >
        {(users) => (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {/* Gate each button on the ACTUAL state (from the optimistic cache), not on isPending:
                  disabling everyone triggers slow per-user Plex cleanup, and gating on isPending left
                  "Select all" dead the whole time. Now each is disabled only when it's already a no-op. */}
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAll.mutate(true)}
                disabled={users.length > 0 && users.every((u) => u.enabled)}
              >
                Select all
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAll.mutate(false)}
                disabled={users.every((u) => !u.enabled)}
              >
                Select none
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => sync.mutate()}
                disabled={sync.isPending}
              >
                {sync.isPending && (
                  <Loader2 className="animate-spin" aria-hidden="true" />
                )}
                Re-sync from plex.tv
              </Button>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead className="text-right">Gets a row</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell>
                      <p className="font-medium">{user.display_name || user.username}</p>
                      {!user.enabled && (
                        <p className="mt-0.5 text-sm font-normal text-muted-foreground">{offReason(user)}</p>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {user.user_type === "managed" && (
                          <Badge variant="secondary">managed</Badge>
                        )}
                        {user.user_type === "owner" && (
                          <Badge variant="outline">
                            owner — never restricted
                          </Badge>
                        )}
                        {user.cold_start && (
                          <Badge
                            variant="warning"
                            title="Not enough watch history yet — starting from popular titles"
                          >
                            New viewer
                          </Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right">
                      <Switch
                        checked={user.enabled}
                        onCheckedChange={(enabled) =>
                          patchUser.mutate({ id: user.id, patch: { enabled } })
                        }
                        aria-label={`Give ${user.username} a row`}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
