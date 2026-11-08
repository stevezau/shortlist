import { BlurSaveField } from "@/components/user-detail/blur-save-field";
import type { User } from "@/lib/types";

/** The tag THEIR requests already carry in Radarr/Sonarr, for a Your requests row to read them by.
 *  Not {@link UserRequestTag}: that is the tag Shortlist WRITES on titles it requests for them; this
 *  is the one it READS when the row's pattern doesn't render to it (a kid whose requests are tagged
 *  `children`, say). */
export function UserRequestedByTag({ user }: { user: User }) {
  return (
    <BlurSaveField
      user={user}
      field="requested_by_tag"
      id="user-requested-by-tag"
      label="Tag on their requests in Radarr/Sonarr"
      placeholder="e.g. children"
      maxLength={64}
      errorText="Couldn’t save this tag. Try again."
      help="For a Your requests row when you tag requests yourself, e.g. children. For Overseerr, choose their account above instead."
      helpClassName="text-sm text-muted-foreground"
    />
  );
}
