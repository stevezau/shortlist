import type { User } from "@/lib/types";

/** The one state vocabulary for a person, shared by the Users list and the person's own page. */
export type UserState = "on" | "paused" | "off";

/**
 * Whether the engine skips this account for its Plex Restriction Profile: restricted AND profiled,
 * exactly as `context_builder.py` decides. The profile alone is not enough: the two flags come from
 * different plex.tv endpoints, and a profiled account not reported restricted still gets its rows built.
 */
export function profileBlocksRows(user: User): boolean {
  return Boolean(user.restricted && user.restriction_profile);
}

/**
 * What a run will do for this person. `enabled` is "do they get a row at all"; `prefs.paused` is
 * "temporarily skipped, row taken off Home" and only matters while enabled. A restriction profile on a restricted
 * account is Off whatever `enabled` says: Plex refuses hide rules for it, so the engine builds no NEW
 * row for it. Rows already on Plex stay until a run removes them.
 */
export function userState(user: User): UserState {
  if (!user.enabled || profileBlocksRows(user)) return "off";
  return user.prefs?.paused ? "paused" : "on";
}
