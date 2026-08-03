import type { User } from "@/lib/types";

/** Turn a Plex username into the name the rest of the app calls that person. */
export type DisplayNameLookup = (username: string) => string;

/**
 * A username → display-name resolver built from the users list.
 *
 * Some payloads carry a bare Plex username where the UI wants a person: the Requests inbox stores
 * `wanters`/`why[].user` as `UserProfile.username` (`engine/rows.py`), which is the raw Plex login.
 * Everywhere else the app shows `display_name || username` — the owner's nickname, else the name
 * Tautulli knows them by, else the username (`User.display_name`). This closes that gap client-side,
 * from the users list the page already has.
 *
 * A username with no match resolves to itself: someone removed from the server, or a row queued
 * before they were synced, must still read as a person rather than as a blank.
 */
export function displayNameLookup(
  users: User[] | undefined,
): DisplayNameLookup {
  const byUsername = new Map<string, string>();
  for (const user of users ?? []) {
    if (user.username) {
      byUsername.set(user.username, user.display_name || user.username);
    }
  }
  return (username) => byUsername.get(username) || username;
}
