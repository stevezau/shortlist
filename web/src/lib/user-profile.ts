import type { User } from "@/lib/types";

/** Plex restriction-profile helpers.
 *
 * Lives in `lib/` rather than beside the badge that renders it: a file that exports both components
 * and plain functions breaks Fast Refresh, and this one is imported by two pages that render no
 * badge at all.
 */
/** The human name of a Plex restriction preset, for copy that names what the owner actually set. */
const PROFILE_NAMES: Record<string, string> = {
  little_kid: "Younger Kid",
  older_kid: "Older Kid",
  teen: "Teen",
};

/** The same name from the bare profile key, for data that is not a `User` (the Privacy accounts). */
export function profileLabel(key: string): string {
  return PROFILE_NAMES[key] ?? key;
}

export function profileName(user: User): string {
  return profileLabel(user.restriction_profile ?? "");
}

/** A Plex account's kind as the owner reads it — Users and Privacy name it the same way. */
export const USER_TYPE_LABEL: Record<string, string> = { owner: "Owner", managed: "Managed", shared: "Shared" };
