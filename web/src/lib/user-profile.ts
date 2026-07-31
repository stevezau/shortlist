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

export function profileName(user: User): string {
  const key = user.restriction_profile ?? "";
  return PROFILE_NAMES[key] ?? key;
}
