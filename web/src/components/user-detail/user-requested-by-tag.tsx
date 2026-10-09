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
      label="Tag their requests already carry"
      placeholder="e.g. children"
      maxLength={64}
      errorText="Couldn’t save this tag. Try again."
      help="Only for a Your requests row: finds what they asked for when their tag isn’t the row’s pattern, e.g. children. Leave blank to use the pattern."
      helpClassName="text-sm text-muted-foreground"
    />
  );
}
