import { BlurSaveField } from "@/components/user-detail/blur-save-field";
import type { User } from "@/lib/types";

/**
 * What to call this person in a row title.
 *
 * Plex usernames are often an email or a handle nobody actually uses, and `{user}` put that straight
 * onto a Home screen. The nickname always wins; blank falls back to the name their watch history has for them,
 * then their Plex username. It never touches their slug, so their row's label — and the share
 * filters that hide it from everyone else — are unaffected.
 */
export function UserNickname({ user }: { user: User }) {
  return (
    <BlurSaveField
      user={user}
      field="nickname"
      id="user-nickname"
      label="Nickname (optional)"
      placeholder={user.friendly_name || user.username}
      maxLength={255}
      errorText="Couldn’t save this nickname. Try again."
      help={
        <>
          Used for <code>{"{user}"}</code> in row names. Leave blank to use{" "}
          {user.friendly_name ? "the name from their watch history" : "their Plex username"}. Saving renames existing Plex rows;
          privacy is unchanged.
        </>
      }
    />
  );
}
