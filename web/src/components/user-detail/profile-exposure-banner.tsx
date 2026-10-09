import { Card } from "@/components/ui/card";
import { LastRunExposure, ProfileFixSteps } from "@/components/user-detail/off-banner";
import { useAccountExposure } from "@/components/user-detail/use-account-exposure";
import { rowsNotTheirs } from "@/lib/privacy-attention";
import type { User } from "@/lib/types";
import { personName } from "@/lib/user-names";
import { profileName } from "@/lib/user-profile";

/**
 * Shown on the page of a person who is NOT Off but has a Restriction Profile and can see rows that
 * are not theirs: Plex refuses hide rules for the profile, so the Privacy page's "How" link and the
 * Users list's "Fix in Plex" both land here. Says nothing unless the live reading, or failing that
 * the last run, found rows showing.
 */
export function ProfileExposureBanner({ user }: { user: User }) {
  const { live, exposed, lastRun, isPending } = useAccountExposure(user);
  const profile = user.restriction_profile ? profileName(user) : "";
  if (!profile) return null;

  const name = personName(user);

  if (live ? exposed === 0 : isPending || lastRun === 0) return null;

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
            <LastRunExposure name={name} lastRun={lastRun} />{" "}
            Plex rejects hide rules for accounts with a Restriction Profile.
          </>
        )}
      </div>
      <ProfileFixSteps name={name} profile={profile} />
    </Card>
  );
}
