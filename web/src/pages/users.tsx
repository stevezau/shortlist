import { Users as UsersIcon } from "lucide-react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { UserAvatar } from "@/components/user-avatar";
import { UserBadges } from "@/components/user-badges";
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
import { formatHitRate, timeAgo } from "@/lib/format";
import { usePatchUser, useUsers } from "@/lib/queries";

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
  const patchUser = usePatchUser();

  return (
    <div>
      <PageHeader
        icon={UsersIcon}
        title="Users"
        subtitle="Everyone on your Plex server. Turn a user on to give them a nightly Picked-for-You row."
      />

      <QueryBoundary
        query={usersQuery}
        skeleton={<UsersSkeleton />}
        isEmpty={(users) => users.length === 0}
        empty={
          <EmptyState
            title="No users yet"
            hint="Rowarr hasn't imported any Plex users. Finish the setup wizard, or check the Plex connection under Settings."
          />
        }
      >
        {(users) => (
          <div className="overflow-hidden rounded-xl border">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>User</TableHead>
                  <TableHead>Type</TableHead>
                  <TableHead>Watch history</TableHead>
                  <TableHead>Last run</TableHead>
                  <TableHead title="Share of Rowarr's picks this person has watched">
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
                      >
                        <UserAvatar name={user.username} size="sm" />
                        <span className="group-hover:underline">
                          {user.username}
                        </span>
                      </Link>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        <UserBadges
                          user={user}
                          emptyFallback={
                            <span className="text-sm text-muted-foreground">
                              —
                            </span>
                          }
                        />
                      </div>
                    </TableCell>
                    <TableCell className="text-muted-foreground tabular-nums">
                      {user.history_depth} titles
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {timeAgo(user.last_run_at)}
                    </TableCell>
                    <TableCell className="text-muted-foreground tabular-nums">
                      {formatHitRate(user.hit_rate)}
                    </TableCell>
                    <TableCell className="text-right">
                      <Switch
                        checked={user.enabled}
                        onCheckedChange={(enabled) =>
                          patchUser.mutate({ id: user.id, patch: { enabled } })
                        }
                        aria-label={`Rowarr row for ${user.username}`}
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
