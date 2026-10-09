import type { User } from "@/lib/types";

/**
 * A complete User row, so a new required field on `User` is one edit here rather than one per test
 * file. Pass only what the test is about.
 */
export function makeUser(patch: Partial<User> = {}): User {
  return {
    manage_sharing: true,
    id: 1,
    username: "sarah",
    slug: "sarah",
    user_type: "shared",
    restricted: false,
    enabled: true,
    cold_start: false,
    history_depth: 0,
    last_run_at: null,
    request_tag: "",
    requested_by_tag: "",
    picks_watched_30d: null,
    last_pick_watched_at: null,
    nickname: "",
    friendly_name: "",
    display_name: "",
    avatar_url: "",
    plex_account_id: 0,
    restriction_profile: "",
    unhidden_rows: 0,
    departed: false,
    preview_titles: [],
    prefs: {},
    ...patch,
  };
}
