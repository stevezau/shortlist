import { Clock } from "lucide-react";
import type { ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { BackLink } from "@/components/back-link";
import { OwnerNote } from "@/components/owner-note";
import { RestrictedNote } from "@/components/restricted-note";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { Segmented } from "@/components/segmented";
import { BlockedSeedsList } from "@/components/user-detail/blocked-seeds";
import { RecentRuns } from "@/components/user-detail/recent-runs";
import { UserDetailHeader } from "@/components/user-detail/user-detail-header";
import { UserNickname } from "@/components/user-detail/user-nickname";
import { UserRequestTag } from "@/components/user-detail/user-request-tag";
import { UserRowsSection } from "@/components/user-detail/user-row-card";
import { UserSharing } from "@/components/user-detail/user-sharing";
import { PickOutcomes } from "@/components/user-detail/pick-outcomes";
import { WatchHistory } from "@/components/user-detail/watch-history";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useUsers } from "@/lib/queries";
import type { User } from "@/lib/types";

function SectionHeading({ children }: { children: ReactNode }) {
  return <h2 className="text-lg font-semibold">{children}</h2>;
}

type UserTab = "rows" | "runs" | "settings" | "history";

const TABS: UserTab[] = ["rows", "runs", "settings", "history"];

export function UserDetailBody({ user }: { user: User }) {
  // In the URL, not component state. Someone arriving from the dashboard's "who watched what" wants
  // the watched view, and a link is the only way to say so — `?tab=` also survives a refresh and the
  // back button, which local state does not.
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab") as UserTab | null;
  const tab: UserTab = asked && TABS.includes(asked) ? asked : "rows";
  const setTab = (next: UserTab) => {
    // `replace`, so flicking between tabs does not fill the back button with them — Back should
    // return to where you came from, which is the dashboard.
    setParams(next === "rows" ? {} : { tab: next }, { replace: true });
  };

  return (
    <div className="space-y-8">
      <UserDetailHeader user={user} />

      {user.user_type === "owner" && <OwnerNote />}
      <RestrictedNote user={user} />

      <Segmented
        options={[
          { value: "rows", label: "Rows" },
          { value: "runs", label: "Runs" },
          { value: "settings", label: "Settings" },
          // "Watched" rather than "Watch History": the tab now holds two different things — what
          // they did with SHORTLIST'S picks, and everything they have ever watched on Plex. The old
          // label described only the second.
          { value: "history", label: "Watched" },
        ]}
        value={tab}
        onChange={(value) => setTab(value as UserTab)}
      />

      {tab === "rows" && (
        <section className="space-y-3">
          <SectionHeading>Their personal rows</SectionHeading>
          <p className="text-sm text-muted-foreground">
            Their current picks, and why each was chosen. Shared rows are the
            same for everyone, so they live under Rows.
          </p>
          <UserRowsSection user={user} />
        </section>
      )}

      {tab === "runs" && (
        <section className="space-y-3">
          <div className="flex items-center justify-between">
            <SectionHeading>Runs that included this person</SectionHeading>
            <Button asChild variant="ghost" size="sm">
              <Link to="/runs">
                <Clock aria-hidden="true" />
                All runs
              </Link>
            </Button>
          </div>
          <p className="text-sm text-muted-foreground">
            A run builds rows for everyone at once, so this is the subset that
            covered {user.display_name || user.username} — each showing what
            happened to <em>their</em> row, not whether the run as a whole
            succeeded.
          </p>
          <RecentRuns userId={user.id} userSlug={user.slug} />
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
            <SectionHeading>Plex sharing</SectionHeading>
            <UserSharing user={user} />
          </section>

          <section className="space-y-3">
            <SectionHeading>Blocked titles</SectionHeading>
            <p className="text-sm text-muted-foreground">
              Shortlist builds someone&rsquo;s picks by looking for things
              similar to what they recently watched. Block a title and it stays
              in their watch history but stops being used that way. Use it for a
              one-off that isn&rsquo;t really them: a film watched for someone
              else, a genre they don&rsquo;t want more of.
            </p>
            <BlockedSeedsList user={user} />
          </section>
        </div>
      )}

      {tab === "history" && (
        <div className="space-y-6">
          {/* Shortlist's picks FIRST. This is the question the app exists to answer for this person,
              and it is the one the page could not answer at all — it could show what was delivered
              and what they had watched, but never whether the recommendations were seen out. */}
          <section className="space-y-3">
            <SectionHeading>What they did with their picks</SectionHeading>
            <p className="text-sm text-muted-foreground">
              Titles Shortlist put in one of their rows that they then played,
              and how far they got.
            </p>
            <Card>
              <CardContent className="pt-6">
                <PickOutcomes userId={user.id} />
              </CardContent>
            </Card>
          </section>

          <section className="space-y-3">
            <SectionHeading>Everything they&rsquo;ve watched</SectionHeading>
            <p className="text-sm text-muted-foreground">
              Their whole Plex history &mdash; the same set every recommendation
              is filtered against.
            </p>
            <Card>
              <CardContent className="pt-6">
                <WatchHistory userId={user.id} user={user} />
              </CardContent>
            </Card>
          </section>
        </div>
      )}
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
