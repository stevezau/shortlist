import { BlurSaveField } from "@/components/user-detail/blur-save-field";
import type { User } from "@/lib/types";

/** Per-user request tag: the label added in Sonarr/Radarr to titles requested for this person. */
export function UserRequestTag({ user }: { user: User }) {
  return (
    <BlurSaveField
      user={user}
      field="request_tag"
      id="user-request-tag"
      label="Request tag (optional)"
      placeholder="e.g. sarah"
      maxLength={64}
      errorText="Couldn’t save this tag. Try again."
      help="Added in Sonarr/Radarr to titles Shortlist requests for this person, alongside your global and row tags. Leave blank for none."
    />
  );
}
