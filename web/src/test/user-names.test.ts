import { describe, expect, it } from "vitest";

import type { User } from "@/lib/types";
import { displayNameLookup } from "@/lib/user-names";

function user(overrides: Partial<User>): User {
  return {
    manage_sharing: true,
    id: 1,
    plex_account_id: 100,
    username: "sarah_p",
    slug: "sarah_p",
    nickname: "",
    friendly_name: "",
    display_name: "",
    avatar_url: "",
    user_type: "shared",
    restricted: false,
    restriction_profile: "",
    unhidden_rows: 0,
    departed: false,
    enabled: true,
    cold_start: false,
    request_tag: "",
    prefs: {},
    history_depth: 0,
    last_run_at: null,
    hit_rate: null,
    preview_titles: [],
    ...overrides,
  };
}

describe("displayNameLookup", () => {
  it("resolves a username to the display name the rest of the app shows", () => {
    const nameOf = displayNameLookup([
      user({ username: "sarah_p", display_name: "Sarah" }),
    ]);
    expect(nameOf("sarah_p")).toBe("Sarah");
  });

  it("returns the username itself when nobody matches", () => {
    // A person removed from the server, or a request queued before they were synced: the name must
    // still read as a person, never as a blank chip.
    const nameOf = displayNameLookup([
      user({ username: "sarah_p", display_name: "Sarah" }),
    ]);
    expect(nameOf("someone_else")).toBe("someone_else");
  });

  it("falls back to the username when the server sends a blank display name", () => {
    const nameOf = displayNameLookup([
      user({ username: "mike", display_name: "" }),
    ]);
    expect(nameOf("mike")).toBe("mike");
  });

  it("resolves every name to itself before the users list has loaded", () => {
    // The page never waits on the users query, so an undefined list has to behave like the old
    // username-only rendering rather than emptying every name.
    const nameOf = displayNameLookup(undefined);
    expect(nameOf("sarah_p")).toBe("sarah_p");
  });

  it("matches on the username, not the slug or the display name", () => {
    const nameOf = displayNameLookup([
      user({ username: "Sarah P", slug: "sarah_p", display_name: "Sarah" }),
    ]);
    expect(nameOf("Sarah P")).toBe("Sarah");
    expect(nameOf("sarah_p")).toBe("sarah_p");
  });
});
