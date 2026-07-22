import { Clock } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { BackLink } from "@/components/back-link";
import { OwnerNote } from "@/components/owner-note";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { RecentRuns } from "@/components/user-detail/recent-runs";
import { UserDetailHeader } from "@/components/user-detail/user-detail-header";
import { UserNickname } from "@/components/user-detail/user-nickname";
import { UserRequestTag } from "@/components/user-detail/user-request-tag";
import { UserRowsSection } from "@/components/user-detail/user-row-card";
import { WatchHistory } from "@/components/user-detail/watch-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";

function SectionHeading({ children }: { children: ReactNode }) {
  return <h2 className="text-lg font-semibold">{children}</h2>;
}

function UserDetailBody({ user }: { user: User }) {
  return (
    <div className="space-y-8">
      <UserDetailHeader user={user} />

      {user.user_type === "owner" && <OwnerNote />}

      <section className="space-y-3">
        <SectionHeading>Their personal rows</SectionHeading>
        <p className="text-sm text-muted-foreground">
          Each per-person row {user.username} gets, with its current picks and
          why each was chosen. Customize any of them for this person only.
          Shared "popular on this server" rows aren&rsquo;t listed here —
          they&rsquo;re the same for everyone and are managed under Rows.
        </p>
        <UserRowsSection user={user} />
      </section>

      <section className="space-y-3">
        <SectionHeading>What to call them</SectionHeading>
        <UserNickname user={user} />
      </section>

      <section className="space-y-3">
        <SectionHeading>Requests</SectionHeading>
        <UserRequestTag user={user} />
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
