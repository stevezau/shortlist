import { Link } from "react-router-dom";

import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
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
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
        <p className="text-sm text-muted-foreground">
          Everyone on your Plex server. Turn a user on to give them a nightly
          Picked-for-You row.
        </p>
      </header>

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
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>User</TableHead>
                <TableHead>Badges</TableHead>
                <TableHead>History</TableHead>
                <TableHead>Last run</TableHead>
                <TableHead>Hit rate</TableHead>
                <TableHead className="text-right">Enabled</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((user) => (
                <TableRow key={user.id}>
                  <TableCell>
                    <Link
                      to={`/users/${user.id}`}
                      className="rounded-sm font-medium hover:underline"
                    >
                      {user.username}
                    </Link>
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
                    {user.history_depth} items
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {timeAgo(user.last_run_at)}
                  </TableCell>
                  <TableCell className="text-muted-foreground">
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
        )}
      </QueryBoundary>
    </div>
  );
}
