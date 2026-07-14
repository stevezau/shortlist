import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useEffect, useRef } from "react";

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
import { queryKeys, usePatchUser, useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";

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

  const setAll = useMutation({
    mutationFn: async ({
      users,
      enabled,
    }: {
      users: User[];
      enabled: boolean;
    }) => {
      for (const user of users) {
        if (user.enabled !== enabled) {
          await api.patchUser(user.id, { enabled });
        }
      }
    },
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

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
    if (!users.some((u) => u.enabled)) setAllMutate({ users, enabled: true });
  }, [sync.isSuccess, users, setAllMutate]);

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-primary/40 bg-primary/10 p-4 text-sm">
        <p className="font-medium text-primary">Heads up, server owner</p>
        <p className="mt-1 text-muted-foreground">
          Plex cannot hide collections from the server owner — your own Home
          will show every user's row. Tip: watch on a non-owner account.
        </p>
      </div>

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
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAll.mutate({ users, enabled: true })}
                disabled={setAll.isPending}
              >
                Select all
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setAll.mutate({ users, enabled: false })}
                disabled={setAll.isPending}
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
                  <TableHead>Badges</TableHead>
                  <TableHead>History</TableHead>
                  <TableHead className="text-right">Gets a row</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {users.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell className="font-medium">
                      {user.username}
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
                          <Badge variant="warning">cold start</Badge>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {user.history_depth > 0
                        ? `${user.history_depth} items`
                        : "unknown yet"}
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
