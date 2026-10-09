import type { User } from "@/lib/types";

/** The one state vocabulary for a person, shared by the Users list and the person's own page. */
export type UserState = "on" | "paused" | "off";

/**
 * What a run will do for this person. `enabled` is "do they get a row at all"; `prefs.paused` is
 * "temporarily skipped, row taken off Home" and only matters while enabled. A restriction profile is
 * Off whatever `enabled` says: Plex refuses hide rules for a profiled account, so the engine builds
 * no row for it.
 */
export function userState(user: User): UserState {
  if (!user.enabled || user.restriction_profile) return "off";
  return user.prefs?.paused ? "paused" : "on";
}
