import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Users as UsersIcon } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { toast } from "sonner";

import { apiErrorMessage } from "@/lib/api";

import { MutationAlert } from "@/components/mutation-alert";
import { OwnerNote } from "@/components/owner-note";
import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
import {
  ColdStartBadge,
  RestrictedBadge,
  UserTypeBadge,
} from "@/components/user-badges";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
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
import { profileName } from "@/lib/user-profile";
import type { User } from "@/lib/types";
import { formatHitRate, timeAgo } from "@/lib/format";
import {
  queryKeys,
  useSetAllUsersEnabled,
  usePatchUser,
  useUsers,
} from "@/lib/queries";

function UsersSkeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-12 w-full" />
      ))}
    </div>
  );
}

export function UsersPage() {
  const usersQuery = useUsers();
  const navigate = useNavigate();
  const patchUser = usePatchUser();

  /**
   * Toggle one person, and say so immediately.
   *
   * Turning someone OFF removes their rows from Plex and re-merges every account's share filter
   * before the request returns — seconds of real work on a shared server. Waiting for the response
   * to give feedback made the click look ignored, and the only thing that eventually appeared was a
   * generic job toast ("Remove a disabled person's rows") that named the machinery rather than the
   * decision. This names the person and the consequence, up front, and resolves in place.
   */
  const toggleUser = (user: User, enabled: boolean) => {
    const who = user.display_name || user.username;
    const id = `user-toggle-${user.id}`;
    toast.loading(enabled ? `Turning on ${who}…` : `Turning off ${who}…`, {
      id,
      description: enabled
        ? "They get a row on the next run. Restoring their access to the shared rows now."
        : "Removing their rows from Plex and updating everyone's share filters.",
    });
    patchUser.mutate(
      { id: user.id, patch: { enabled } },
      {
        onSuccess: () =>
          toast.success(enabled ? `${who} is on` : `${who} is off`, {
            id,
            description: enabled
              ? "Their row is rebuilt on the next run."
              : "Their rows are off Plex and everyone else's filters are updated.",
            action: {
              label: "Jobs",
              onClick: () => navigate("/jobs"),
            },
          }),
        onError: (error) =>
          toast.error(`Couldn't turn ${enabled ? "on" : "off"} ${who}`, {
            id,
            description: apiErrorMessage(error, "The change was not saved."),
          }),
      },
    );
  };
  const setAll = useSetAllUsersEnabled();
  const queryClient = useQueryClient();
  const [confirmDisableOpen, setConfirmDisableOpen] = useState(false);
  const [confirmEnableOpen, setConfirmEnableOpen] = useState(false);
  const userCount = usersQuery.data?.length ?? 0;

  // The wizard syncs on its way past, but an install that finished setup had NO way to pull the
  // roster again — so someone newly invited to Plex, and the owner's own row, never appeared.
  const sync = useMutation({
    mutationFn: api.syncUsers,
    // A sync is a Plex WRITER (it renames collections when a nickname changes), so it waits for a
    // run to finish rather than writing underneath it. Without this the button would just stop
    // spinning and nothing would visibly change — the roster is unchanged because it has not run yet.
    onSuccess: (result) =>
      result.queued
        ? toast.success("Sync queued", {
            description:
              "A run is using Plex right now, so this will happen the moment it finishes.",
          })
        : toast.success(
            result.added || result.updated
              ? `Synced — ${result.added} new, ${result.updated} updated`
              : "Synced — no changes",
          ),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

  return (
    <div>
      <PageHeader
        icon={UsersIcon}
        title="Users"
        subtitle="Everyone on your Plex server. Turn someone on and they start getting the rows you've built."
        actions={
          // Wraps: three buttons need 345px in one line and ran off a 320px screen.
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={() => sync.mutate()}
              loading={sync.isPending}
              title="Pull the latest users from Plex and friendly names from Tautulli (if connected)"
            >
              <RefreshCw aria-hidden="true" />
              Sync users
            </Button>
            {/* Neither trigger carries `loading` — both merely OPEN a dialog; the mutation (and its
                loading state) belongs to the confirm button inside it. */}
            <Button
              variant="outline"
              onClick={() => setConfirmEnableOpen(true)}
            >
              Enable all
            </Button>
            <Button
              variant="outline"
              onClick={() => setConfirmDisableOpen(true)}
            >
              Disable all
            </Button>
          </div>
        }
      />

      {sync.isError && (
        <MutationAlert
          className="mb-4"
          error={sync.error}
          fallback="Couldn’t reach plex.tv to refresh the user list. Try again."
          onRetry={() => sync.mutate()}
        />
      )}

      {/* The Switch reads the server's answer, so a rejected PATCH just snaps it back — which is
          indistinguishable from the click never landing unless we say what happened. */}
      {patchUser.isError && (
        <MutationAlert
          className="mb-4"
          error={patchUser.error}
          fallback="Couldn’t change that user. Try again."
          onRetry={() => {
            const last = patchUser.variables;
            if (last) patchUser.mutate(last);
          }}
        />
      )}
      {setAll.isError && (
        <MutationAlert
          className="mb-4"
          error={setAll.error}
          fallback="Couldn’t update everyone at once. Try again."
        />
      )}

      <Dialog open={confirmEnableOpen} onOpenChange={setConfirmEnableOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn on all {userCount} users?</DialogTitle>
            {/* "each user gets a row" was an over-claim: an account with a Plex restriction
                profile is skipped (Plex hides every collection from it), and a person still needs
                to be in some row's audience. */}
            <DialogDescription>
              Everyone here is switched on, so each of them gets their own
              private row the next time the rows they&rsquo;re in are built.
              Accounts Plex won&rsquo;t show collections to &mdash; the ones
              badged with a restriction profile &mdash; stay skipped. Turn
              anyone back off individually whenever you like.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setConfirmEnableOpen(false)}>
              Cancel
            </Button>
            <Button
              loading={setAll.isPending && setAll.variables === true}
              onClick={() =>
                setAll.mutate(true, {
                  onSuccess: () => setConfirmEnableOpen(false),
                })
              }
            >
              Enable all
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={confirmDisableOpen} onOpenChange={setConfirmDisableOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn off every user?</DialogTitle>
            {/* NOT "share filters are left untouched" — turning someone off queues a privacy pass
                that WRITES their share filter (users.py `remove_users_rows` → `queue_privacy_sync`),
                which is what stops a disabled account seeing the shared rows too. */}
            <DialogDescription>
              This turns everyone off and takes every Picked-for-You row off
              Plex right away. Each account&rsquo;s Plex sharing settings are
              updated to match, so nobody is left seeing a row that no longer
              belongs to them. The record of how sharing looked before you
              installed Shortlist is kept, so a full uninstall can still put it
              back &mdash; and turning anyone back on rebuilds their row on its
              next run.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="ghost"
              onClick={() => setConfirmDisableOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              loading={setAll.isPending && setAll.variables === false}
              onClick={() =>
                setAll.mutate(false, {
                  onSuccess: () => setConfirmDisableOpen(false),
                })
              }
            >
              Disable all
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <QueryBoundary
        query={usersQuery}
        skeleton={<UsersSkeleton />}
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Shortlist hasn’t found anyone on your server. Press “Sync users” above to ask plex.tv again — and if that turns up nothing, check the server address and token in Settings → Connections, on the Plex card."
          />
        }
      >
        {(users) => (
          <div className="space-y-4">
            {users.some((user) => user.user_type === "owner") && <OwnerNote />}
            <div className="overflow-hidden rounded-xl border">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead>User</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Watch history</TableHead>
                    <TableHead>Last run</TableHead>
                    <TableHead title="Share of Shortlist's picks this person has watched">
                      Picks watched
                    </TableHead>
                    <TableHead className="text-right">Enabled</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {users.map((user) => (
                    <TableRow key={user.id} className="group">
                      <TableCell>
                        <Link
                          to={`/users/${user.id}`}
                          className="flex items-center gap-3 rounded-sm font-medium text-foreground group-hover:text-primary"
                          title={`Plex username: ${user.username}`}
                        >
                          <UserAvatar name={user.username} size="sm" />
                          <span className="group-hover:underline">
                            {user.display_name || user.username}
                          </span>
                        </Link>
                      </TableCell>
                      <TableCell>
                        <span className="flex flex-wrap items-center gap-1.5">
                          <UserTypeBadge user={user} />
                          <RestrictedBadge user={user} />
                        </span>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {/* "New viewer" belongs HERE, next to the number it explains — it's a
                            state, not a type, and in the Type column it read as one. */}
                        <span className="flex flex-wrap items-center gap-2">
                          <span className="tabular-nums">
                            {user.history_depth} titles
                          </span>
                          <ColdStartBadge user={user} />
                        </span>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {timeAgo(user.last_run_at)}
                      </TableCell>
                      <TableCell className="text-muted-foreground tabular-nums">
                        {formatHitRate(user.hit_rate)}
                      </TableCell>
                      <TableCell className="text-right">
                        {/* Gated on the PRESET, not on `restricted` — plex.tv sets that for every Plex
                            Home user, so keying on it greyed out ordinary managed accounts that can
                            perfectly well have a row (#20). */}
                        <Switch
                          checked={user.enabled && !user.restriction_profile}
                          disabled={Boolean(user.restriction_profile)}
                          title={
                            user.restriction_profile
                              ? `Plex's ${profileName(user)} restriction profile hides every collection from this account — set the profile to None in Plex to enable`
                              : undefined
                          }
                          onCheckedChange={(enabled) =>
                            toggleUser(user, enabled)
                          }
                          aria-label={`Shortlist row for ${user.username}`}
                        />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
