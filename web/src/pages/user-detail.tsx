import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Clock, X } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import { Link, useParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { OwnerNote } from "@/components/owner-note";
import { RestrictedNote } from "@/components/restricted-note";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { RecentRuns } from "@/components/user-detail/recent-runs";
import { UserDetailHeader } from "@/components/user-detail/user-detail-header";
import { UserNickname } from "@/components/user-detail/user-nickname";
import { UserRequestTag } from "@/components/user-detail/user-request-tag";
import { UserRowsSection } from "@/components/user-detail/user-row-card";
import { WatchHistory } from "@/components/user-detail/watch-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import { queryKeys, useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";

function SectionHeading({ children }: { children: ReactNode }) {
  return <h2 className="text-lg font-semibold">{children}</h2>;
}

type UserTab = "rows" | "settings" | "history";

function UserDetailBody({ user }: { user: User }) {
  const [tab, setTab] = useState<UserTab>("rows");

  return (
    <div className="space-y-8">
      <UserDetailHeader user={user} />

      {user.user_type === "owner" && <OwnerNote />}
      <RestrictedNote user={user} />

      <Segmented
        options={[
          { value: "rows", label: "Rows" },
          { value: "settings", label: "Settings" },
          { value: "history", label: "Watch History" },
        ]}
        value={tab}
        onChange={(value) => setTab(value as UserTab)}
      />

      {tab === "rows" && (
        <section className="space-y-3">
          <SectionHeading>Their personal rows</SectionHeading>
          <p className="text-sm text-muted-foreground">
            Each per-person row {user.display_name || user.username} gets, with
            its current picks and why each was chosen. Customize any of them for
            this person only. Shared "popular on this server" rows aren&rsquo;t
            listed here — they&rsquo;re the same for everyone and are managed
            under Rows.
          </p>
          <UserRowsSection user={user} />
        </section>
      )}

      {tab === "settings" && (
        <div className="space-y-8">
          <section className="space-y-3">
            <SectionHeading>What to call them</SectionHeading>
            <UserNickname user={user} />
          </section>

          <section className="space-y-3">
            <SectionHeading>Requests</SectionHeading>
            <UserRequestTag user={user} />
          </section>

          <section className="space-y-3">
            <SectionHeading>Blocked seeds</SectionHeading>
            <p className="text-sm text-muted-foreground">
              Titles on this list are never used as recommendation seeds for
              this person — even if they watched them. Use this when a watch
              shouldn&rsquo;t influence their picks (a one-off genre they
              don&rsquo;t want more of).
            </p>
            <BlockedSeedsList user={user} />
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
      )}

      {tab === "history" && (
        <section className="space-y-3">
          <SectionHeading>Watch history</SectionHeading>
          <Card>
            <CardContent className="pt-6">
              <WatchHistory userId={user.id} />
            </CardContent>
          </Card>
        </section>
      )}
    </div>
  );
}

function BlockedSeedsList({ user }: { user: User }) {
  const queryClient = useQueryClient();
  const blocked: number[] =
    ((user.prefs as Record<string, unknown>)?.blocked_seeds as number[]) ?? [];
  const unblock = useMutation({
    mutationFn: (tmdbId: number) => api.unblockSeed(user.id, tmdbId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.users }),
  });

  if (blocked.length === 0) {
    return (
      <p className="text-sm text-muted-foreground/70">
        No blocked seeds. Titles can be blocked from the trace page ("How we
        picked") or by editing this user's preferences via the API.
      </p>
    );
  }

  return (
    <ul className="space-y-1">
      {blocked.map((tmdbId) => (
        <li
          key={tmdbId}
          className="flex items-center justify-between rounded-md border px-3 py-2 text-sm"
        >
          <span className="font-mono text-muted-foreground">TMDB {tmdbId}</span>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => unblock.mutate(tmdbId)}
            title="Unblock this seed"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        </li>
      ))}
    </ul>
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
