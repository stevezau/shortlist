import { Card } from "@/components/ui/card";
import { ProfileFixSteps } from "@/components/user-detail/off-banner";
import { rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { usePrivacyStatus } from "@/lib/queries";
import type { User } from "@/lib/types";
import { profileName } from "@/lib/user-profile";

/**
 * Shown on the page of a person who is NOT Off but has a Restriction Profile and can see rows that
 * are not theirs: Plex refuses hide rules for the profile, so the Privacy page's "How" link and the
 * Users list's "Fix in Plex" both land here. Says nothing unless the live reading, or failing that
 * the last run, found rows showing.
 */
export function ProfileExposureBanner({ user }: { user: User }) {
  const privacy = usePrivacyStatus();
  const name = user.display_name || user.username;
  const account = privacy.data?.accounts.find((a) => a.user_id === user.id);
  const live = account && privacy.data && !privacy.data.error && !privacy.data.rows_error;
  const exposed = live ? rowsNotHidden(account, privacy.data) : 0;
  const lastRun = user.unhidden_rows;
  const profile = user.restriction_profile ? profileName(user) : "";
  if (!profile) return null;

  if (live ? exposed === 0 : privacy.isPending || lastRun === 0) return null;

  return (
    <Card data-testid="profile-exposure-banner" className="overflow-hidden p-0">
      <div className="bg-warning/10 px-6 py-3 text-sm text-warning">
        {live ? (
          <>
            <span className="font-medium">
              {name} {rowsNotTheirs(exposed)}.
            </span>{" "}
            Plex rejects hide rules for accounts with a Restriction Profile.
          </>
        ) : (
          <>
            <span className="font-medium">
              Couldn&rsquo;t check live. The last run found {name} could see {lastRun}{" "}
              {lastRun === 1 ? "collection" : "collections"} that aren&rsquo;t theirs.
            </span>{" "}
            Plex rejects hide rules for accounts with a Restriction Profile.
          </>
        )}
      </div>
      <ProfileFixSteps name={name} profile={profile} />
    </Card>
  );
}
